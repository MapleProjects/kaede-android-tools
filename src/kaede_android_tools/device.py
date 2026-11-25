"""Device layer: adb primitives plus accessibility selectors via uiautomator2."""

from __future__ import annotations

import contextlib
import io
import re
import subprocess
import sys
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass

BOUNDS_RE = re.compile(r"\[(-?\d+),(-?\d+)\]\[(-?\d+),(-?\d+)\]")

KIND = {
    "android.widget.Button": "btn",
    "android.widget.ImageButton": "btn",
    "android.widget.EditText": "in",
    "android.widget.AutoCompleteTextView": "in",
    "android.widget.TextView": "txt",
    "android.widget.ImageView": "img",
    "android.widget.CheckBox": "chk",
    "android.widget.RadioButton": "rad",
    "android.widget.Switch": "sw",
    "android.widget.ToggleButton": "sw",
    "android.widget.Spinner": "sel",
    "android.widget.SeekBar": "bar",
    "android.webkit.WebView": "web",
    "android.view.View": "view",
    "android.view.ViewGroup": "group",
}

# Packages whose foreground surface is a browser and therefore has a CDP endpoint.
BROWSER_HINTS = (
    "chrome",
    "chromium",
    "browser",
    "vivaldi",
    "brave",
    "firefox",
    "edge",
    "opera",
    "duckduckgo",
)


@dataclass
class Element:
    ref: int
    text: str
    kind: str
    cls: str
    rid: str
    bounds: tuple[int, int, int, int]
    clickable: bool
    editable: bool
    checked: bool | None = None
    index: int = 0

    @property
    def center(self) -> tuple[int, int]:
        l, t, r, b = self.bounds
        return (l + r) // 2, (t + b) // 2

    def signature(self) -> tuple[str, str, str]:
        return (self.text, self.rid, self.cls)

    def line(self) -> str:
        flags = []
        if self.clickable:
            flags.append("tap")
        if self.editable:
            flags.append("type")
        if self.checked is not None:
            flags.append("on" if self.checked else "off")
        label = self.text.replace("\n", " ").strip()
        if len(label) > 70:
            label = label[:67] + "..."
        extra = f" {' '.join(flags)}" if flags else ""
        return f"#{self.ref} {self.kind} {label!r}{extra}"


def _adb(*args: str, timeout: int = 30) -> str:
    proc = subprocess.run(
        ["adb", *args], capture_output=True, text=True, timeout=timeout
    )
    return (proc.stdout or "") + (proc.stderr or "")


class Device:
    """Thin, stateful wrapper. One instance per server process."""

    def __init__(self, serial: str | None = None) -> None:
        self.serial = serial
        self._d = None
        self._refs: dict[int, Element] = {}
        self._last_pkg = ""

    # ---------------------------------------------------------------- plumbing
    def d(self):
        if self._d is None:
            import uiautomator2 as u2

            # The first connect pushes and installs the on-device agent and
            # adbutils prints progress to stdout, which would corrupt the MCP
            # stdio stream. Route that window to stderr instead.
            with contextlib.redirect_stdout(io.StringIO()):
                self._d = u2.connect(self.serial) if self.serial else u2.connect()
        return self._d

    def adb(self, *args: str, timeout: int = 30) -> str:
        cmd = ["adb"]
        if self.serial:
            cmd += ["-s", self.serial]
        return _adb(*cmd[1:], *args, timeout=timeout)

    def screen_size(self) -> tuple[int, int]:
        info = self.d().info
        return int(info["displayWidth"]), int(info["displayHeight"])

    def current_app(self) -> tuple[str, str]:
        try:
            cur = self.d().app_current()
            return cur.get("package", "?"), cur.get("activity", "?")
        except Exception:
            return "?", "?"

    def keyboard_visible(self) -> bool:
        out = self.adb("shell", "dumpsys", "input_method")
        return "mInputShown=true" in out

    def focus_window(self) -> tuple[str, str]:
        """The window the system currently gives input to.

        Differs from current_app() when an overlay steals focus: permission
        dialogs, the intent resolver ("open with"), or a floating prompt.
        """
        try:
            out = self.adb("shell", "dumpsys", "window", "windows")
        except Exception:
            return "", ""
        for line in out.splitlines():
            if "mCurrentFocus=" in line:
                token = line.split("mCurrentFocus=")[-1].strip()
                # Window{hash u0 pkg/cls} or Window{hash pkg/cls}
                inner = token[token.find("{") + 1 : token.rfind("}")] if "{" in token else token
                parts = inner.split()
                target = parts[-1] if parts else ""
                if "/" in target:
                    pkg, _, cls = target.partition("/")
                    return pkg, cls
                return target, ""
        return "", ""

    def overlay(self) -> str:
        """Name of a focus-stealing overlay above the foreground app, if any."""
        pkg, cls = self.focus_window()
        app_pkg, _ = self.current_app()
        if pkg and pkg != app_pkg:
            return f"{pkg}/{cls}"
        return ""

    def is_browser(self, package: str) -> bool:
        return any(h in package.lower() for h in BROWSER_HINTS)

    # ------------------------------------------------------------- hierarchy
    def raw_xml(self) -> str:
        return self.d().dump_hierarchy()

    def elements(self) -> list[Element]:
        xml = self.raw_xml()
        try:
            root = ET.fromstring(xml)
        except ET.ParseError:
            return []
        out: list[Element] = []
        seq = 0
        for node in root.iter("node"):
            m = BOUNDS_RE.match(node.get("bounds", ""))
            if not m:
                continue
            l, t, r, b = (int(g) for g in m.groups())
            if r <= l or b <= t:
                continue
            cls = node.get("class", "")
            text = (node.get("text") or "").strip()
            desc = (node.get("content-desc") or "").strip()
            label = text or desc
            clickable = node.get("clickable") == "true"
            editable = cls.endswith("EditText")
            scrollable = node.get("scrollable") == "true"
            if not label and not clickable and not editable and not scrollable:
                continue
            seq += 1
            out.append(
                Element(
                    ref=0,
                    text=label,
                    kind=KIND.get(cls, cls.split(".")[-1].lower() or "view"),
                    cls=cls,
                    rid=node.get("resource-id", ""),
                    bounds=(l, t, r, b),
                    clickable=clickable or scrollable,
                    editable=editable,
                    checked=(
                        None
                        if node.get("checkable") != "true"
                        else node.get("checked") == "true"
                    ),
                    index=seq,
                )
            )
        for i, el in enumerate(out, start=1):
            el.ref = i
        self._refs = {el.ref: el for el in out}
        return out

    def snapshot(self, full: bool = False, text_only: bool = False) -> str:
        pkg, act = self.current_app()
        self._last_pkg = pkg
        w, h = self.screen_size()
        els = self.elements()
        if text_only:
            seen: list[str] = []
            for e in els:
                if e.text and e.text not in seen:
                    seen.append(e.text)
            return "\n".join(seen) if seen else "(no text on screen)"
        header = [
            f"app={pkg} act={act} {w}x{h} kb={'on' if self.keyboard_visible() else 'off'}"
        ]
        # An overlay that stole focus is invisible in the app's own tree, and it
        # is usually the thing blocking progress: permission prompts, "open
        # with" sheets, "continue to the app" dialogs.
        over = self.overlay()
        if over:
            header[0] += f" OVERLAY={over} (a dialog has focus; act on it first)"
        if self.is_browser(pkg):
            header[0] += " browser=1 (use web tool for page content)"
        # Actionable by default. Labels alone are noise for most tasks and
        # text= selectors still reach them, since resolve() scans the full tree.
        shown = els if full else [e for e in els if e.clickable or e.editable]
        rows = [e.line() for e in shown]
        if not full and len(rows) > 60:
            rows = rows[:60] + [f"... {len(els) - 60} more (see full=true)"]
        if not rows:
            rows = ["(nothing actionable on screen)"]
        return "\n".join(header + rows)

    # -------------------------------------------------------------- resolving
    def resolve(self, target: str) -> Element | None:
        """Accept '#3', 'text=Foo', '~Foo', 'id=...' or 'x,y'."""
        target = (target or "").strip()
        if not target:
            return None
        if target.startswith("#"):
            try:
                ref = int(target[1:])
            except ValueError:
                return None
            return self._resolve_ref(ref)
        if "," in target and target.replace(",", "").replace(" ", "").isdigit():
            x, y = (int(v) for v in target.split(","))
            return Element(0, f"{x},{y}", "point", "", "", (x, y, x, y), True, False)
        if target.startswith("id="):
            needle = target[3:]
            return self._find(lambda e: needle in e.rid)
        if target.startswith("~"):
            needle = target[1:].lower()
            return self._find(lambda e: needle in e.text.lower())
        if target.startswith("text="):
            needle = target[5:]
            return self._find(lambda e: e.text == needle) or self._find(
                lambda e: needle.lower() in e.text.lower()
            )
        return self._find(lambda e: target.lower() in e.text.lower())

    def _find(self, pred) -> Element | None:
        for el in self.elements():
            if pred(el):
                return el
        return None

    def _resolve_ref(self, ref: int) -> Element | None:
        """Refs are per-snapshot; re-anchor on the live tree.

        Text is a bad anchor for inputs: a field whose value we just typed no
        longer matches its own old signature. For editable nodes anchor on
        resource id plus the nearest centre instead.
        """
        old = self._refs.get(ref)
        els = self.elements()
        if old is None:
            return None
        if old.editable:
            pool = [e for e in els if e.editable and e.rid == old.rid]
            if not pool:
                pool = [e for e in els if e.editable]
            return self._nearest(pool, old) or None
        same = [e for e in els if e.signature() == old.signature()]
        if not same:
            return None
        return self._nearest(same, old)

    def _nearest(self, pool: list[Element], old: Element) -> Element | None:
        if not pool:
            return None
        if len(pool) == 1:
            return pool[0]
        ocx, ocy = old.center
        return min(pool, key=lambda e: (e.center[0] - ocx) ** 2 + (e.center[1] - ocy) ** 2)

    # ---------------------------------------------------------------- actions
    def tap(self, el: Element) -> None:
        x, y = el.center
        self.d().click(x, y)

    def long_press(self, el: Element, duration: int = 1000) -> None:
        x, y = el.center
        self.d().long_click(x, y, duration=duration / 1000)

    def swipe_xy(self, x1: int, y1: int, x2: int, y2: int, duration: float = 0.3) -> None:
        self.d().swipe(x1, y1, x2, y2, duration=duration)

    def refresh(self) -> str:
        """Make a stale list load again.

        Browsers reload properly. Native apps do not expose a refresh verb, so
        the honest fallback is a pull gesture from the top, which is what
        Gmail, Play Store, Telegram and most list screens listen for.
        """
        pkg, _ = self.current_app()
        if self.is_browser(pkg):
            return "browser"
        w, h = self.screen_size()
        self.swipe_xy(w // 2, int(h * 0.20), w // 2, int(h * 0.72), duration=0.5)
        time.sleep(1.2)
        return "pull"

    def swipe_direction(self, direction: str, ratio: float = 0.6) -> None:
        w, h = self.screen_size()
        cx, cy = w // 2, h // 2
        dx, dy = int(w * ratio / 2), int(h * ratio / 2)
        moves = {
            "up": (cx, cy + dy, cx, cy - dy),
            "down": (cx, cy - dy, cx, cy + dy),
            "left": (cx + dx, cy, cx - dx, cy),
            "right": (cx - dx, cy, cx + dx, cy),
        }
        if direction not in moves:
            raise ValueError(f"direction must be one of {sorted(moves)}")
        self.swipe_xy(*moves[direction])

    def key(self, name: str) -> None:
        self.d().press(name)

    def launch(self, package: str) -> None:
        self.d().app_start(package, wait=True)

    def open_url(self, url: str) -> None:
        self.adb("shell", "am", "start", "-a", "android.intent.action.VIEW", "-d", url)

    def focused_text(self) -> str:
        xml = self.raw_xml()
        try:
            root = ET.fromstring(xml)
        except ET.ParseError:
            return ""
        for node in root.iter("node"):
            if node.get("focused") == "true":
                return (node.get("text") or "").strip()
        return ""

    # ------------------------------------------------------------- stability
    def wait_idle(self, budget: float = 4.0, interval: float = 0.25) -> bool:
        """Block until two consecutive hierarchies match, or budget runs out."""
        deadline = time.time() + budget
        prev = self.raw_xml()
        while time.time() < deadline:
            time.sleep(interval)
            cur = self.raw_xml()
            if cur == prev:
                return True
            prev = cur
        return False

    def wait_for(self, target: str, timeout: float = 8.0) -> bool:
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self.resolve(target):
                return True
            time.sleep(0.3)
        return False
