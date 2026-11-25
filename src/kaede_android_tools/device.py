"""Device layer: adb primitives plus accessibility selectors via uiautomator2."""

from __future__ import annotations

import contextlib
import io
import json
import os
import re
import shlex
import subprocess
import sys
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass

BOUNDS_RE = re.compile(r"\[(-?\d+),(-?\d+)\]\[(-?\d+),(-?\d+)\]")

# Aliases live in a file, not in the server's environment: a phone changes
# transport (USB serial one day, an address:port the next) and a pin that has
# to be edited and a process restarted to follow it is a pin nobody updates.
ALIAS_FILE = os.path.expanduser(
    os.environ.get("KAEDE_DEVICE_FILE") or "~/.config/kaede-android-tools/devices.json"
)


# Keys that configure the tool itself. Everything else in the file is a
# device alias, so listing aliases must not hand these back as if they were
# phones: `use text=browsers` would try to adb-connect to a list.
SETTINGS_KEYS = ("browsers", "submit_selectors", "protect")


def aliases() -> dict[str, str]:
    """Named device targets from the config file, lowercased.

    Empty when there is none. Settings are filtered out: the file holds both,
    because one file for "which phone" and another for "how to drive it" is a
    file nobody remembers to edit.
    """
    try:
        with open(ALIAS_FILE, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    return {
        str(k).lower(): str(v)
        for k, v in data.items()
        if str(k).lower() not in SETTINGS_KEYS and not isinstance(v, (list, dict))
    }


# One file answers both "which phone" and "how this build wants to be driven".
# Nothing app- or site-specific belongs in the module: a selector found on one
# build misleads on every other.
CONFIG_FILE = ALIAS_FILE

DEFAULT_CONFIG = {
    # Tried in order and joined with commas into one CSS selector, so a page
    # whose own button text is ambiguous still has a deterministic submit.
    "submit_selectors": [
        "#idSIButton9",
        "input[type=submit]",
        "button[type=submit]",
        "[role=button][aria-label*='Next' i]",
    ],
    # Folders whose files the tool may not edit while the owner is driving it.
    "protect": ["~/.config/kaede-android-tools", "~/.config/mimocode"],
}


def config() -> dict:
    """The tool's settings, with the defaults filled in."""
    data = DEFAULT_CONFIG.copy()
    try:
        with open(CONFIG_FILE, encoding="utf-8") as fh:
            loaded = json.load(fh)
    except (OSError, ValueError):
        return data
    if isinstance(loaded, dict):
        data.update({k: v for k, v in loaded.items() if v is not None})
    return data


def protect_patterns() -> list[str]:
    """Globs that the self-edit guard refuses to touch without an explicit yes."""
    raw = config().get("protect") or []
    if isinstance(raw, str):
        raw = [raw]
    out: list[str] = []
    for item in raw:
        expanded = os.path.expanduser(str(item))
        out.append(expanded)
        out.append(expanded.rstrip("/") + "/*")
    return out


# A verification code is a bare run of digits. Four to eight is what logins,
# banks and mail providers use; a longer run is a phone number, a card number
# or an amount, and those must not be offered as a code. The boundaries are
# word characters, not just digits: a username like "player1222" ends in a
# four-digit run, and offering that as a code is how the wrong string gets
# copied into a login box.
CODE_RE = re.compile(r"(?<!\w)(\d{4,8})(?!\w)")

RECORD_RE = re.compile(r"^\s+NotificationRecord\(.*?pkg=(\S+)")
RECORD_ID_RE = re.compile(r"\bid=(-?\d+)")
WHEN_RE = re.compile(r"\bwhen=(\d{10,13})")
# The type in front of the value is whatever class the app put there, and it is
# not always String: a code highlighted by the sender arrives as
# SpannableString, and matching only String dropped the one notification that
# carried the code while reporting "no code in the notifications".
_CS = r"\w*(?:String|CharSequence|Spanned)"
TITLE_RE = re.compile(rf"^\s+android\.title={_CS} \((.*)\)$")
TEXT_RE = re.compile(rf"^\s+android\.(?:big)?text={_CS} \((.*)\)$")
# An InboxStyle notification keeps its body in textLines, one entry per line,
# and its android.text only says how many messages arrived.
TEXTLINES_RE = re.compile(r"^\s+android\.textLines=\S+")
TEXTLINE_RE = re.compile(r"^\s+\[(\d+)\] (.*)$")
# A dumpsys section header, e.g. "  History Notification List:".
SECTION_RE = re.compile(r"^  [A-Z][\w .]*:$")

TASK_ID_RE = re.compile(r"^\s+id=(\d+) userId=")
TASK_INTENT_RE = re.compile(r"^\s+baseIntent=Intent \{ (.*) \}$")
TASK_CMP_RE = re.compile(r"cmp=([\w.]+)/([\w.$]+)")
TASK_ACT_RE = re.compile(r"act=(\S+)")

# A label can arrive double-encoded: UTF-8 bytes decoded as latin-1, which is
# what a Flutter dialog did to "sesión" -> "sesiÃ³n". Repairing it is what makes
# text= selectors match the words the screen actually shows.
MOJIBAKE_HINTS = ("Ã", "Â")

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

# Packages that are browsers, and so surfaces with a CDP endpoint. A fallback
# for a build with no config: the real list comes from "browsers", because what
# is installed on one phone is not a rule for another.
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
    focused: bool = False
    index: int = 0
    hot: tuple[int, int] | None = None
    listens: bool = True
    area: tuple[int, int, int, int] | None = None
    # The caption above an input. A form built out of plain Views puts the
    # title in its own node with no link to the field, so a screen with four
    # EditTexts reads as four anonymous boxes and the agent types into the
    # third because it counts rows instead of reading names.
    label: str = ""

    def contains(self, point: tuple[int, int]) -> bool:
        l, t, r, b = self.bounds
        return l <= point[0] <= r and t <= point[1] <= b

    @property
    def center(self) -> tuple[int, int]:
        l, t, r, b = self.bounds
        return (l + r) // 2, (t + b) // 2

    @property
    def handle(self) -> str:
        """How to name a node the tree leaves nameless.

        An icon button carries no text, no content-desc and often no
        resource-id, so position is the only handle it has; the row prints it
        as @x,y, which is also a valid act target.
        """
        x, y = self.hot or self.center
        rid = self.rid.rsplit("/", 1)[-1]
        return f"@{x},{y}" + (f" {rid}" if rid else "")

    def signature(self) -> tuple[str, str, str]:
        return (self.text, self.rid, self.cls)

    def line(self) -> str:
        flags = []
        if self.clickable:
            flags.append("tap" if self.listens else "no-handler")
        if self.editable:
            flags.append("type")
        if self.checked is not None:
            flags.append("on" if self.checked else "off")
        label = self.text.replace("\n", " ").strip()
        if len(label) > 70:
            label = label[:67] + "..."
        shown = repr(label) if label else self.handle
        extra = f" {' '.join(flags)}" if flags else ""
        # A field is identified by its caption, not by what is typed in it: an
        # empty one used to print a coordinate and a filled one a value, so
        # neither said which box it was.
        if self.label and (not label or label != self.label):
            caption = self.label.replace("\n", " ").strip()[:40]
            shown = f"({caption!r} empty)" if not label else f"{shown} ({caption})"
        return f"#{self.ref} {self.kind} {shown}{extra}"


def _hit_area(node, parents: dict, bounds_re):
    """The node a finger would press for this name, or None if nothing does.

    A screen can carry a name and not listen to it: a bare View with a
    content-desc and no handler, or a title inside a clickable skeleton whose
    real hit area is elsewhere. Walking up to the nearest ancestor that takes
    a touch turns the name into an answer, and refusing when there is none is
    what stops the tool from reporting a tap that did nothing.
    """
    cur = node
    for _ in range(6):
        if cur is None:
            return None
        if cur.get("clickable") == "true" and bounds_re.match(cur.get("bounds", "")):
            return cur
        cur = parents.get(cur)
    return None


def _shrink(path: str, max_width: int) -> str:
    """Downscale a screenshot and re-encode it as JPEG, beside the original.

    A full-resolution PNG of a 1080x2400 screen is ~2 MB, which overflows the
    attachment budget of the model reading it, so a caller asking for a picture
    of the screen gets a file it cannot open. JPEG at half resolution stays
    legible for labels and lands around 100 KB. Pillow is optional: without it
    the original path comes back untouched rather than no picture at all.
    """
    try:
        from PIL import Image
    except ImportError:  # pragma: no cover
        return path
    out = os.path.splitext(path)[0] + ".jpg"
    try:
        with Image.open(path) as img:
            if img.width > max_width:
                height = round(img.height * max_width / img.width)
                img = img.resize((max_width, height), Image.LANCZOS)
            img.convert("RGB").save(out, "JPEG", quality=80, optimize=True)
    except Exception:  # a corrupt capture must not lose the caller its path
        return path
    return out


def _clean(text: str) -> str:
    """Undo a latin-1 decode of UTF-8 text, keeping anything it cannot fix."""
    text = (text or "").strip()
    if not any(hint in text for hint in MOJIBAKE_HINTS):
        return text
    try:
        return text.encode("latin-1").decode("utf-8")
    except (UnicodeEncodeError, UnicodeDecodeError):
        return text


def _one_line(text: str) -> str:
    return re.sub(r"\s+", " ", text or "").strip()


def _target_matches(target: str, text: str) -> bool:
    """Apply the see() target grammar to one label, for screens without refs."""
    want = (target or "").strip()
    if not want:
        return False
    if want.startswith("text="):
        exact = want[5:]
        return text == exact or exact.lower() in text.lower()
    if want.startswith("~"):
        return want[1:].lower() in text.lower()
    return want.lower() in text.lower()


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
        self._attached_cache: tuple[float, list[str]] | None = None

    # ---------------------------------------------------------------- plumbing
    def attached(self, ttl: float = 3.0) -> list[str]:
        """Serials adb can talk to right now, cached for a few seconds."""
        now = time.time()
        if self._attached_cache and now - self._attached_cache[0] < ttl:
            return self._attached_cache[1]
        out = _adb("devices")
        found = [
            line.split()[0]
            for line in out.splitlines()[1:]
            if len(line.split()) >= 2 and line.split()[1] == "device"
        ]
        self._attached_cache = (now, found)
        return found

    def devices(self) -> list[dict]:
        """Every attached device with its model, and which one is in use."""
        out = _adb("devices", "-l")
        active = self.target(quiet=True)
        found = []
        for line in out.splitlines()[1:]:
            parts = line.split()
            if len(parts) < 2 or parts[1] != "device":
                continue
            model = next(
                (p.split(":", 1)[1] for p in parts if p.startswith("model:")), "?"
            )
            found.append(
                {
                    "serial": parts[0],
                    "model": model.replace("_", " "),
                    "active": parts[0] == active,
                }
            )
        return found

    def device_list(self) -> str:
        attached = self.devices()
        names = aliases()
        lines = []
        if attached:
            for dev in attached:
                mark = "*" if dev["active"] else " "
                alias = next(
                    (k for k, v in names.items() if v == dev["serial"].split(":")[0]),
                    "",
                )
                lines.append(
                    f"{mark} {dev['serial']}  {dev['model']}"
                    + (f"  (alias {alias})" if alias else "")
                )
        else:
            lines.append("no devices attached")
        if names:
            lines.append("aliases:")
            lines += [f"  {k} -> {v}" for k, v in sorted(names.items())]
        lines.append("switch with act action=use text=<serial or alias>")
        return "\n".join(lines)

    def _mdns_address(self, host: str) -> str:
        """Current wireless-debugging address for `host`, from mDNS.

        Wireless debugging hands out a new port every time it is re-enabled, so
        an alias that stored the port goes stale within a day; the address that
        adb itself advertises does not.
        """
        out = _adb("mdns", "services")
        for line in out.splitlines():
            parts = line.split()
            if len(parts) < 3 or "_adb-tls-connect._tcp" not in line:
                continue
            address = parts[-1]
            if address.startswith(host):
                return address
        return ""

    def use(self, needle: str) -> str:
        """Point every later call at one device, now or after a reconnect."""
        wanted = (needle or "").strip()
        if not wanted:
            return "use needs text=<serial or alias>; act devices lists them"
        address = aliases().get(wanted.lower(), wanted)
        attached = self.attached()
        if address not in attached:
            if ":" not in address:
                address = self._mdns_address(address) or address
            if ":" in address and address not in attached:
                _adb("connect", address)
                self._attached_cache = None
                attached = self.attached()
        match = next((s for s in attached if s == address), None) or next(
            (s for s in attached if address.lower() in s.lower()), None
        )
        if match is None:
            known = ", ".join(attached) or "none"
            return f"no device matches {wanted!r} (attached: {known})"
        self.serial = match
        self._d = None
        self._refs = {}
        self._attached_cache = None
        return f"active device: {match}"

    def target(self, quiet: bool = False) -> str | None:
        """The serial to use, re-checked because a phone changes transport.

        A USB serial stops existing the moment the cable is pulled and the
        phone comes back as an address:port under wireless debugging. Honouring
        the configured serial forever is how a reachable phone answers "device
        not found" until someone edits the config and restarts the server.
        """
        devices = self.attached()
        if self.serial in devices:
            return self.serial
        if len(devices) == 1:
            self.serial = devices[0]
            self._d = None
            return self.serial
        if not devices:
            return self.serial
        if quiet:
            return None
        # Two phones and no choice made: picking one silently would drive the
        # wrong device, which is worse than stopping.
        raise RuntimeError(
            "several devices are attached ("
            + ", ".join(devices)
            + "); choose one with act action=use text=<serial or alias>"
        )

    def d(self):
        if self._d is None:
            import uiautomator2 as u2

            # The first connect pushes and installs the on-device agent and
            # adbutils prints progress to stdout, which would corrupt the MCP
            # stdio stream. Route that window to stderr instead.
            serial = self.target()
            with contextlib.redirect_stdout(io.StringIO()):
                self._d = u2.connect(serial) if serial else u2.connect()
        return self._d

    def adb(self, *args: str, timeout: int = 30) -> str:
        serial = self.target()
        if serial:
            return _adb("-s", serial, *args, timeout=timeout)
        return _adb(*args, timeout=timeout)

    def screen_size(self) -> tuple[int, int]:
        info = self.d().info
        return int(info["displayWidth"]), int(info["displayHeight"])

    def screen_on(self) -> bool:
        """Whether the display is awake. A sleeping display dumps an empty tree."""
        try:
            return bool(self.d().info.get("screenOn", True))
        except Exception:
            return True

    def locked(self) -> bool:
        """Whether a secure keyguard is up. `input` does not get past a PIN."""
        try:
            return "deviceLocked=1" in self.adb("shell", "dumpsys", "trust")
        except Exception:
            return False

    def screenshot(self, path: str, max_width: int = 0) -> str:
        """Write a PNG to `path`. The agent reads it with its own file tool.

        With `max_width` the image is downscaled and re-encoded as JPEG next to
        `path`, because a 1080x2400 PNG is ~2 MB and overflows the model's
        attachment budget the moment it is read. Text stays legible at half
        resolution; only a crop keeps fine detail, and a crop is the caller's
        decision, not ours.
        """
        try:
            self.d().screenshot(path)
        except Exception:
            out = subprocess.run(
                ["adb", "-s", self.serial, "exec-out", "screencap", "-p"]
                if self.serial
                else ["adb", "exec-out", "screencap", "-p"],
                capture_output=True,
            ).stdout
            with open(path, "wb") as fh:
                fh.write(out)
        if not max_width:
            return path
        return _shrink(path, max_width)

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
        extra = config().get("browsers") or []
        if isinstance(extra, str):
            extra = [extra]
        hints = tuple(str(h).lower() for h in extra) + BROWSER_HINTS
        return any(h in package.lower() for h in hints)

    # ------------------------------------------------------------- hierarchy
    def raw_xml(self) -> str:
        return self.d().dump_hierarchy()

    def elements(self) -> list[Element]:
        xml = self.raw_xml()
        try:
            root = ET.fromstring(xml)
        except ET.ParseError:
            return []
        nodes = list(root.iter("node"))
        kids: dict = {n: [c for c in n if c.tag == "node"] for n in nodes}

        def own(node) -> str:
            return _clean(node.get("text")) or _clean(node.get("content-desc"))

        def below(node, depth: int = 5):
            queue = [(c, 1) for c in kids.get(node, [])]
            while queue:
                child, level = queue.pop(0)
                yield child
                if level < depth:
                    queue.extend((g, level + 1) for g in kids.get(child, []))

        out: list[Element] = []
        seq = 0
        parents = {c: n for n in nodes for c in kids[n]}

        def sibling_label(node, box: tuple[int, int, int, int]) -> str:
            """A label beside the node, in the same vertical band.

            Some rows wrap an empty clickable skeleton and put the title in a
            sibling TextView, so the label is not a child.
            """
            parent = parents.get(node)
            if parent is None:
                return ""
            pool = []
            for cand in below(parent, depth=5):
                if cand is node:
                    continue
                text = own(cand)
                m2 = BOUNDS_RE.match(cand.get("bounds", ""))
                if not text or not m2:
                    continue
                bx = tuple(int(g) for g in m2.groups())
                if bx[2] <= bx[0] or bx[3] <= bx[1]:
                    continue
                pool.append((bx, text))
            for bx, text in pool:
                if bx[0] >= box[0] and bx[2] <= box[2] and bx[1] >= box[1] and bx[3] <= box[3]:
                    return text
            for bx, text in pool:
                shared = min(box[3], bx[3]) - max(box[1], bx[1])
                if shared > 0 and shared >= 0.5 * min(box[3] - box[1], bx[3] - bx[1]):
                    return text
            return ""

        for node in nodes:
            m = BOUNDS_RE.match(node.get("bounds", ""))
            if not m:
                continue
            l, t, r, b = (int(g) for g in m.groups())
            if r <= l or b <= t:
                continue
            cls = node.get("class", "")
            clickable = node.get("clickable") == "true"
            editable = cls.endswith("EditText")
            scrollable = node.get("scrollable") == "true"
            interactive = clickable or editable or scrollable
            label = own(node)
            # Rows keep the title in a child and the state in a grandchild
            # switch; reading only the node loses both.
            checked = None
            if node.get("checkable") == "true":
                checked = node.get("checked") == "true"
            else:
                for child in below(node):
                    if child.get("checkable") == "true":
                        checked = child.get("checked") == "true"
                        break
            hot = None
            borrowed = ""
            # A container that took the words of a child it wraps reacts to
            # them; one that took them from a sibling does not, and claiming it
            # can be pressed turns a tap on a title into a broken call.
            if not label and interactive:
                for child in below(node):
                    text = own(child)
                    if text:
                        label = borrowed = text
                        spot = BOUNDS_RE.match(child.get("bounds", ""))
                        if spot and checked is None:
                            # Aim at the label, not the row: a wide row's
                            # centre is often dead space. A switch is the
                            # exception, its skin is inert.
                            sl, st, sr, sb = (int(g) for g in spot.groups())
                            hot = ((sl + sr) // 2, (st + sb) // 2)
                        break
            # A list never owns a label, it borrows the first child's. Taking
            # it hides the row and shifts every later ref by one.
            if scrollable:
                if not interactive:
                    continue
                label = ""
            elif not label and interactive:
                label = sibling_label(node, (l, t, r, b))
            if not label and not interactive:
                continue
            hit_area = None
            if clickable:
                listens = True
            else:
                # The dump reports clickable past a node's own bounds, so the
                # flag alone promises no handler: a drawn arrow can be a sixth
                # of its hit area, and a title inside a clickable skeleton
                # looks like a button while doing nothing.
                area = _hit_area(node, parents, BOUNDS_RE)
                listens = area is not None
                if area is not None and area.get("clickable") != "true":
                    # An ancestor that takes the touch without carrying the
                    # words is not this label's control.
                    listens = False
                if area is not None and area is not node:
                    m2 = BOUNDS_RE.match(area.get("bounds", ""))
                    if m2:
                        hl, ht, hr, hb = (int(g) for g in m2.groups())
                        hot = ((hl + hr) // 2, (ht + hb) // 2)
                        hit_area = (hl, ht, hr, hb)
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
                    checked=checked,
                    focused=node.get("focused") == "true",
                    index=seq,
                    hot=hot,
                    listens=listens,
                    area=hit_area,
                )
            )
        kept = self._unfolded(out, self._window_height(root))
        self._caption(kept, out)
        for i, el in enumerate(kept, start=1):
            el.ref = i
        self._refs = {el.ref: el for el in kept}
        return kept

    @staticmethod
    def _caption(kept: list[Element], out: list[Element]) -> None:
        """Attach the caption that belongs to each input.

        Forms built from plain Views caption a field with a separate node and
        no parent link, so a field arrives anonymous and the caller has to
        count boxes to aim at one. The caption sits above the field in the same
        row band, but it does not have to overlap it horizontally: a phone
        field put a country picker between its caption and its box, so the
        label ends far left of the field it names.

        The right caption is therefore the closest one above, measured
        left-aligned, and a caption already taken by a nearer field is not
        reused. A caption that sits inside no field's row band is ignored.
        """
        captions = [
            e for e in out if e.text and not e.clickable and not e.editable
        ]
        used: set[int] = set()
        for field in kept:
            if not field.editable:
                continue
            fl, ft, _, _ = field.bounds
            best = None
            best_cost = None
            for cap in captions:
                cl, ct, cr, cb = cap.bounds
                if id(cap) in used or cb > ft:
                    continue
                # How far above the field's top edge the caption's bottom sits.
                gap = ft - cb
                if gap > 160:
                    continue
                # Closest above wins, not left-aligned: a country picker or a
                # unit suffix sits nearer the field's left edge than its own
                # caption does, and picking on alignment labelled the email
                # field "+593".
                cost = (gap, abs(cl - fl))
                if best_cost is None or cost < best_cost:
                    best, best_cost = cap, cost
            if best is not None:
                used.add(id(best))
                field.label = best.text

    @staticmethod
    def _window_height(root) -> int:
        node = root if root.get("bounds") else next(root.iter("node"), None)
        m = BOUNDS_RE.match(node.get("bounds", "")) if node is not None else None
        return int(m.group(4)) - int(m.group(2)) if m else 0

    @staticmethod
    def _covers(outer: tuple[int, int, int, int], inner: tuple[int, int, int, int]) -> bool:
        ol, ot, orr, ob = outer
        il, it, ir, ib = inner
        return outer != inner and ol <= il and ot <= it and orr >= ir and ob >= ib

    def _unfolded(
        self, els: list[Element], screen_h: int = 0
    ) -> list[Element]:
        """Drop labels folded into a clickable ancestor.

        A row and its title carrying the same text waste a ref and invite the
        wrong pick; on a tie the first node in document order wins. Only a row
        may fold a nameless child: a container spanning most of the screen
        covers every nameless icon, and folding those deletes them from the
        snapshot without a hint.
        """
        kept: list[Element] = []
        for i, e in enumerate(els):
            folded = False
            if not e.text and e.clickable:
                # The bare switch inside a row that already carries the label
                # and the state: the row is the canonical tap target.
                if any(
                    f is not e
                    and f.text
                    and f.clickable
                    and self._covers(f.bounds, e.bounds)
                    and not self._is_container(f.bounds, screen_h)
                    for f in els
                ):
                    continue
            for j, f in enumerate(els):
                if i == j or not f.text or f.text != e.text:
                    continue
                # One row, one line: a label that took the words of a child
                # it wraps is the row the agent should press, and the child is
                # a repeat. This only folds downwards when the child cannot be
                # pressed itself, and only for a row that is not a scrolling
                # surface.
                if (
                    e.clickable
                    and e.listens
                    and f.clickable
                    and not f.listens
                    and f.bounds != e.bounds
                    and self._covers(e.bounds, f.bounds)
                    and not self._is_container(e.bounds, screen_h)
                    and not self._is_scroll(e.cls)
                ):
                    folded = True
                    break
                if not (f.clickable or f.editable):
                    continue
                # A container that took its child's label is a scrolling
                # surface, not the control: folding the button into it leaves a
                # target that goes nowhere or scrolls the wrong distance.
                if self._is_container(f.bounds, screen_h) or self._is_scroll(f.cls):
                    continue
                if self._covers(f.bounds, e.bounds) or (
                    f.bounds == e.bounds and j < i
                ):
                    folded = True
                    break
            if not folded:
                kept.append(e)
        return kept

    @staticmethod
    def _is_container(bounds: tuple[int, int, int, int], screen_h: int) -> bool:
        return bool(screen_h) and (bounds[3] - bounds[1]) >= 0.5 * screen_h

    @staticmethod
    def _is_scroll(cls: str) -> bool:
        """Whether a node is a scrolling surface rather than a control."""
        return "ScrollView" in cls or "RecyclerView" in cls or "ViewPager" in cls

    def snapshot(self, full: bool = False, text_only: bool = False) -> str:
        if not self.screen_on():
            pkg, act = self.current_app()
            return (
                f"screen is OFF ({pkg}/{act}) — the accessibility tree is empty while the "
                "display sleeps, so this is not a blank app. `act key=wake` wakes it; a "
                "secure keyguard then needs the owner to unlock."
            )
        pkg, act = self.current_app()
        self._last_pkg = pkg
        w, h = self.screen_size()
        els = self.elements()
        if not els:
            reason = "keyguard=locked, unlock it first" if self.locked() else "no nodes"
            return f"app={pkg} act={act} {w}x{h} nothing readable ({reason})"
        if text_only:
            # Read top to bottom, not in document order: a list that recycles
            # its rows hands back a body node after the header below it, and the
            # wrong line reads as "the newest".
            seen: list[str] = []
            for e in sorted(els, key=lambda e: (e.bounds[1], e.bounds[0])):
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
        hint = self.showing_crash(els)
        if hint:
            cause = ""
            try:
                cause = self.last_crash()
            except Exception:
                pass
            header.append(f"!! a crash dialog is open ({hint}); act: crash")
            if cause:
                header.append(cause)
        if self.is_browser(pkg):
            header[0] += " browser=1 (use web tool for page content)"
        # Actionable by default. Labels alone are noise for most tasks and
        # text= selectors still reach them, since resolve() scans the full tree.
        shown = els if full else [e for e in els if e.clickable or e.editable]
        nameless = [e for e in shown if not e.text and e.clickable]
        if nameless:
            refs = ", ".join(f"#{e.ref}" for e in nameless)
            header.append(
                f"nameless tappables: {refs} — the tree gives them no text, desc "
                "or id; each row carries its @x,y, and see image=true names an icon"
            )
        budget = max(20, 60 - len(header))
        rows = [e.line() for e in shown]
        if not full and len(rows) > budget:
            rows = rows[:budget] + [f"... {len(els) - budget} more (see full=true)"]
        if not rows:
            rows = [
                "(nothing actionable in the tree) — a dialog or popup window may "
                "be covering the app; see image=true to look, or act diag"
            ]
        return "\n".join(header + rows)

    # -------------------------------------------------------------- resolving
    def resolve(self, target: str, want: str = "") -> Element | None:
        """Accept '#3', 'text=Foo', '~Foo', 'id=...' or 'x,y' (also '@x,y').

        The '@x,y' form is what see() prints for a node that has no label, so
        refusing it here would make the tool reject the only handle it offered.

        `want="tap"` means the caller is going to press the result, so a label
        that no finger can reach is traded for the control that wraps it.
        """
        el = self._lookup(target)
        if el is None or want != "tap":
            return el
        if el.clickable or el.editable:
            return el
        return self._holder(el) or el

    def _holder(self, el: Element) -> Element | None:
        """The smallest clickable node that contains `el`.

        A label and its control carry the same words on the same screen: the
        permission sheet's "Permitir" is a line of text inside a button, and
        the button is the only thing that reacts. Tapping the text's centre
        sometimes lands inside the button and sometimes misses it, which looks
        like a broken tool instead of a coincidentally working tap.
        """
        l, t, r, b = el.bounds
        holders = [
            e
            for e in self.elements()
            if e.clickable
            and e is not el
            and not self._is_scroll(e.cls)
            and e.bounds[0] <= l
            and e.bounds[1] <= t
            and e.bounds[2] >= r
            and e.bounds[3] >= b
        ]
        if not holders:
            return None
        return min(
            holders,
            key=lambda e: (e.bounds[2] - e.bounds[0]) * (e.bounds[3] - e.bounds[1]),
        )

    def _lookup(self, target: str) -> Element | None:
        target = (target or "").strip()
        if not target:
            return None
        if target.startswith("#"):
            try:
                ref = int(target[1:])
            except ValueError:
                return None
            return self._resolve_ref(ref)
        point = target[1:] if target.startswith("@") else target
        if "," in point and point.replace(",", "").replace(" ", "").isdigit():
            x, y = (int(v) for v in point.split(","))
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

    @staticmethod
    def _actionable(e: Element) -> int:
        """How much a node looks like the thing a person means by its label.

        Screens routinely carry the same words twice: a heading and the button
        below it ("Iniciar sesión" as a title and as the submit), or a label
        and the scroll view that wraps it. Returning the first match in tree
        order then hands back the heading, and tapping it does nothing at all
        — which reads as a broken tool rather than a wrong pick.

        Fields first (that is what "type" wants), then real buttons, then
        containers, then plain labels.
        """
        if e.editable:
            return 4
        if e.clickable and not Device._is_scroll(e.cls):
            return 3
        if e.clickable:
            return 2          # a scroll container that took its child's label
        return 1 if e.text else 0

    def _find(self, pred) -> Element | None:
        """The node that best matches `pred`, not merely the first one.

        Ties keep document order, which is what a single unambiguous label
        needs; `max` is stable that way on the first maximum.
        """
        matches = [el for el in self.elements() if pred(el)]
        if not matches:
            return None
        return max(matches, key=self._actionable)

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
            if old.rid:
                pool = [e for e in els if e.editable and e.rid == old.rid]
            else:
                # A labelless field shares its signature with every other one,
                # and an open keyboard adds another: "nearest editable" then
                # lands on whichever box is close, not the one pointed at.
                # Keep the slot, or refuse.
                pool = [
                    e
                    for e in els
                    if e.editable
                    and abs(e.center[0] - old.center[0]) <= 40
                    and abs(e.center[1] - old.center[1]) <= 30
                ]
            return self._nearest(pool, old) or None
        same = [e for e in els if e.signature() == old.signature()]
        if len(same) > 1 and any(e.listens for e in same):
            # The same words can come from a title and a card, or from a
            # setting and the switch that ignores taps: hand back the one that
            # answers to a finger.
            same = [e for e in same if e.listens]
        if not same:
            return None
        # A signature shared by several rows identifies none of them. Anchoring
        # on the nearest one is how everything below drifted a screen at a time,
        # so require the same slot and refuse rather than guess.
        if len(same) > 1:
            slot = [
                e
                for e in same
                if e.cls == old.cls
                and abs(e.center[0] - old.center[0]) <= 40
                and abs(e.center[1] - old.center[1]) <= 30
            ]
            return self._nearest(slot, old) if slot else None
        if old.text or old.rid:
            return self._nearest(same, old)
        # Unlabelled rows (Preference screens, lists of empty ViewGroups) all
        # share the same signature, so "nearest" happily lands on a different
        # row after a navigation. Only accept the same slot.
        slot = [
            e
            for e in same
            if e.cls == old.cls
            and abs(e.center[0] - old.center[0]) <= 40
            and abs(e.center[1] - old.center[1]) <= 30
        ]
        return self._nearest(slot, old) if slot else None

    def _nearest(self, pool: list[Element], old: Element) -> Element | None:
        if not pool:
            return None
        if len(pool) == 1:
            return pool[0]
        ocx, ocy = old.center
        return min(pool, key=lambda e: (e.center[0] - ocx) ** 2 + (e.center[1] - ocy) ** 2)

    def state_of(self, label: str) -> str:
        """Live on/off of the toggle labelled `label`, or '' if it is not one."""
        if not label:
            return ""
        el = self.resolve("text=" + label)
        if el is None or el.checked is None:
            return ""
        return "on" if el.checked else "off"

    # ---------------------------------------------------------------- actions
    def tap(self, el: Element) -> tuple[int, int]:
        x, y = el.hot or el.center
        self.d().click(x, y)
        return x, y

    def long_press(self, el: Element, duration: int = 1000) -> tuple[int, int]:
        x, y = el.hot or el.center
        self.d().long_click(x, y, duration=duration / 1000)
        return x, y

    def swipe_xy(self, x1: int, y1: int, x2: int, y2: int, duration: float = 0.3) -> None:
        self.d().swipe(x1, y1, x2, y2, duration=duration)

    def refresh(self) -> str:
        """Make a stale list load again.

        Browsers reload properly. Native apps expose no refresh verb, so the
        honest fallback is a pull gesture from the top, which is what most list
        screens listen for.
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

    # Key names that adb understands but uiautomator2 does not.
    RAW_KEYS = {"wake": 224, "sleep": 223, "power": 26}

    def key(self, name: str) -> None:
        code = self.RAW_KEYS.get(name.lower())
        if code is not None:
            self.adb("shell", "input", "keyevent", str(code))
            return
        self.d().press(name)

    def launch(self, package: str) -> None:
        """Open a package, resuming its task instead of restarting it.

        A cold start throws away whatever the app was in the middle of. The
        existing task comes forward first; only when there is none does the
        launcher activity get started explicitly. The recents carousel is not
        consulted here: a launch means "open this app", and a card hunt that
        misses leaves the switcher in front of everything.
        """
        if self.current_app()[0] == package:
            return
        if self.ensure_foreground(package, allow_cards=False):
            return
        resolved = self.resolve_component(package)
        if resolved:
            self.adb("shell", "am", "start", "-n", resolved)
            if self.wait_front(package, 6.0):
                return
        self.d().app_start(package, wait=True)

    def open_url(self, url: str, package: str = "", settle: float = 3.0) -> str:
        """Open `url`, in `package` when one is named. Returns the app to land in.

        A bare VIEW intent has no package, so the system answers it with the
        "open with" chooser whenever more than one browser is installed: a
        dialog in front of the page instead of the page. Naming the package
        removes the question.

        The caller gets the app that actually took the intent, because the
        read right after `am start` is the screen before the transition, not
        the page. Waiting for the app to change is what makes the following
        call see a page instead of the previous app's last frame.
        """
        before = self.current_app()[0]
        args = ["shell", "am", "start", "-a", "android.intent.action.VIEW", "-d", url]
        if package:
            args += ["-p", package]
        self.adb(*args)
        deadline = time.time() + settle
        while time.time() < deadline:
            now = self.current_app()[0]
            if now != before and now != "?":
                return now
            time.sleep(0.25)
        return self.current_app()[0]

    def focused_text(self) -> str:
        """What the field holding focus contains.

        Several nodes can report focused="true" at once: the window, the
        keyboard's container and the field itself. Taking the first one made
        the text look empty on a field that was full, so `type` skipped its
        clear and the new value was appended to the old one
        ("246810" + "Ana"). An editable node is the answer when there is one.
        """
        xml = self.raw_xml()
        try:
            root = ET.fromstring(xml)
        except ET.ParseError:
            return ""
        fallback = ""
        for node in root.iter("node"):
            if node.get("focused") != "true":
                continue
            text = (node.get("text") or "").strip()
            if (node.get("class") or "").endswith("EditText"):
                return text
            if text and not fallback:
                fallback = text
        return fallback

    def input_text(self, text: str) -> None:
        """Type through the device input pipeline.

        A WebView input ignores uiautomator2's set_text, which writes through
        the accessibility layer the page never listens to; `input text`
        synthesises real key events at the input-device level and lands.
        """
        self.adb("shell", "input", "text", shlex.quote(text.replace(" ", "%s")))

    def backspace(self, times: int) -> None:
        """Erase `times` characters from whatever holds focus."""
        if times > 0:
            self.adb(
                "shell",
                f"input keyevent {' '.join(['67'] * min(times, 200))}",
            )

    # ------------------------------------------------------------- stability
    def _settled(self, xml: str) -> bool:
        """Whether a dump is a laid-out screen and not a half-painted one.

        A mid-transition dump can be perfectly stable — the window is up, the
        layout has not run — and two identical reads of it are not proof that
        the screen stopped moving. Counting the interactive nodes is cheap and
        catches the empty frame that promises refs to nothing.
        """
        try:
            root = ET.fromstring(xml)
        except ET.ParseError:
            return False
        return sum(1 for n in root.iter("node") if n.get("clickable") == "true") > 0

    def wait_idle(self, budget: float = 4.0, interval: float = 0.25) -> bool:
        """Block until two consecutive hierarchies match, or budget runs out.

        The budget is a deadline, not a dump count: one hierarchy of an
        embedded web view can take longer to fetch than the whole budget on a
        slow link, so a sample-counting loop reports "not settled" for a screen
        that was ready all along. Elapsed time keeps the call bounded whatever
        a dump costs.
        """
        deadline = time.time() + budget
        interval = max(interval, 0.05)
        try:
            prev = self.raw_xml()
        except Exception:
            return False
        while time.time() < deadline:
            time.sleep(interval)
            try:
                cur = self.raw_xml()
            except Exception:
                return False
            if cur == prev and self._settled(cur):
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

    # ------------------------------------------------------- reading codes
    @staticmethod
    def notification_window(dump: str) -> list[str]:
        """The Notification List slice of `dumpsys notification`.

        Everything after it repeats the words title and text for records that
        are no longer posted (history, ranking, zen rules), so the slice has to
        stop at the next section header.

        Indentation cannot be the signal: a long record is wrapped mid-line, so
        its continuation starts in column 0, and treating that as "a section
        ended" cut the list off after the first notification and hid the one
        carrying the code. The list ends where the dump says so, at a section
        header of the form `  Name:`.
        """
        lines = (dump or "").splitlines()
        start = next(
            (i for i, line in enumerate(lines) if line.strip() == "Notification List:"),
            None,
        )
        if start is None:
            return []
        window: list[str] = []
        for line in lines[start + 1 :]:
            if SECTION_RE.match(line):
                break
            window.append(line)
        return window

    def notifications(self, limit: int = 12) -> list[dict]:
        """Posted notifications, newest first, without opening the shade.

        Reading a code off the shade costs an expand, a scroll to the right row
        and a dump; the notification service already holds the same text,
        unredacted, in one call and works with the shade closed.
        """
        dump = self.adb("shell", "dumpsys", "notification", "--noredact") or ""
        records: list[dict] = []
        cur: dict | None = None
        in_lines = False
        for line in self.notification_window(dump):
            started = RECORD_RE.match(line)
            if started:
                cur = {
                    "pkg": started.group(1),
                    "title": "",
                    "text": "",
                    "lines": "",
                    "when": 0,
                }
                records.append(cur)
                in_lines = False
                continue
            if cur is None:
                continue
            title = TITLE_RE.match(line)
            if title:
                cur["title"] = _one_line(title.group(1))
                continue
            body = TEXT_RE.match(line)
            if body:
                cur["text"] = _one_line(body.group(1))
                in_lines = False
                continue
            if TEXTLINES_RE.match(line):
                in_lines = True
                continue
            if in_lines:
                entry = TEXTLINE_RE.match(line)
                if entry:
                    more = _one_line(entry.group(2))
                    # An inbox keeps one message per line and its android.text
                    # only counts them ("2 mensajes nuevos"), so the lines are
                    # the body and the summary must not overwrite them.
                    cur["lines"] = (
                        f"{cur['lines']} {more}".strip() if cur["lines"] else more
                    )
                    continue
                in_lines = False
            stamp = WHEN_RE.search(line)
            if stamp:
                cur["when"] = int(stamp.group(1))
        for rec in records:
            if rec["lines"]:
                rec["text"] = rec["lines"]
            rec.pop("lines", None)
        records.sort(key=lambda r: r["when"], reverse=True)
        return records[:limit]

    def screen_codes(self) -> list[str]:
        """Code-shaped lines of the current screen, in reading order."""
        try:
            elements = self.elements()
        except Exception:
            # The tree can die on its own (agent restart, unplugged cable) and
            # that must not throw away the candidates already read from the
            # notification service.
            return []
        rows = []
        for el in sorted(elements, key=lambda e: (e.bounds[1], e.bounds[0])):
            text = el.text.strip()
            if text and CODE_RE.search(text):
                rows.append(f"{el.bounds[1]:>5}  {_one_line(text)[:100]}")
        return rows

    def otp(self, hint: str | None = None) -> str:
        """Every code-shaped number on offer, with the line it came from.

        A text dump is ordered by layout, not by time, so the newest code can
        read as the oldest and the stale one gets typed. Carrying each
        candidate's own line and timestamp makes the choice checkable.
        """
        needle = (hint or "").strip().lower()
        try:
            notes = self.notifications()
        except Exception:
            notes = []
        if needle:
            notes = [
                n
                for n in notes
                if needle in f"{n['pkg']} {n['title']} {n['text']}".lower()
            ]
        found: list[str] = []
        for note in notes:
            blob = f"{note['title']} {note['text']}".strip()
            for code in dict.fromkeys(CODE_RE.findall(blob)):
                when = (
                    time.strftime("%H:%M", time.localtime(note["when"] / 1000))
                    if note["when"]
                    else "?"
                )
                found.append(f"{code}  notification {note['pkg']} {when}  {blob[:80]}")
        screen = self.screen_codes()
        if not found and not screen:
            lines = ["otp: no code-shaped number on the screen or in the notifications"]
            if notes:
                lines.append("posted notifications, none of them with a code:")
                lines += [
                    f"  {n['pkg']}: {(n['title'] or n['text'])[:70]}" for n in notes[:6]
                ]
            else:
                lines.append("no notifications are posted either")
            return "\n".join(lines)
        lines = [f"otp: {len(found) + len(screen)} candidate(s), newest first"]
        lines += found
        if screen:
            lines.append(
                "on screen, top to bottom (y is the row's top edge); in a mail or "
                "chat list the newest is the last one, and that is the only code "
                "the login will accept:"
            )
            screen[-1] = screen[-1] + "   <- newest on screen"
            lines += screen
        if found and screen:
            lines.append(
                "typing an older code is the usual way this fails: the server "
                "rejects it while the newest one still works"
            )
        return "\n".join(lines)

    # ---------------------------------------------------------------- passkeys
    PASSKEY_TITLE = "bloqueo de pantalla"

    def passkey_prompt(self) -> bool:
        """Whether the screen in front is a cross-device passkey prompt."""
        try:
            texts = [el.text.lower() for el in self.elements() if el.text]
        except Exception:
            return False
        return any(self.PASSKEY_TITLE in t for t in texts)

    def passkey(self, pin: str) -> str:
        """Answer a "screen lock of your other device" passkey prompt.

        A passkey continuation asks for the *other* device's screen lock, and
        without it the field sits there looking like an ordinary PIN box that
        nothing will accept. The field is unlabelled, so the reliable way in is
        to tap the single input the prompt shows; no selector survives a
        rebuild.

        The prompt also offers a skip button, which drops the passkey and keeps
        the password-only session. That is the fallback when no PIN is known,
        never the default: it changes what the account has enrolled.
        """
        if not self.passkey_prompt():
            return "no passkey prompt on screen: nothing to answer"
        field = next(
            (el for el in self.elements() if el.editable and not el.text.strip()),
            None,
        )
        if field is None:
            return "the prompt is up but it shows no field to type into"
        self.tap(field)
        self.adb("shell", "input", "text", shlex.quote(pin))
        self.adb("shell", "input", "keyevent", "66")  # ENTER submits
        self.wait_idle()
        if self.passkey_prompt():
            return (
                f"typed {len(pin)} digits and the prompt is still up: the PIN was "
                "refused or the submit button is separate — check the screen"
            )
        return f"passkey answered with a {len(pin)}-digit PIN"

    # ---------------------------------------------------------- task switching
    def tasks(self) -> list[dict]:
        """Recent tasks with the intent that created each one, newest first.

        The map every switch needs: a package can own several tasks, and only
        the intent separates a launcher task (safe to re-start, it comes back
        with its state) from a VIEW task such as a custom tab (re-starting it
        opens a second copy, so it has to be reshown from recents).
        """
        dump = self.adb("shell", "dumpsys", "activity", "recents") or ""
        found: list[dict] = []
        cur: dict | None = None
        for line in dump.splitlines():
            fresh = TASK_ID_RE.match(line)
            if fresh:
                if cur:
                    found.append(cur)
                cur = {"id": int(fresh.group(1)), "pkg": "", "cls": "", "action": ""}
                continue
            if cur is None:
                continue
            intent = TASK_INTENT_RE.match(line)
            if not intent:
                continue
            blob = intent.group(1)
            component = TASK_CMP_RE.search(blob)
            if component:
                cur["pkg"], cur["cls"] = component.group(1), component.group(2)
            action = TASK_ACT_RE.search(blob)
            if action:
                cur["action"] = action.group(1)
        if cur:
            found.append(cur)
        return [task for task in found if task["pkg"]]

    def wait_front(self, package: str, timeout: float = 4.0) -> bool:
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self.current_app()[0] == package:
                return True
            time.sleep(0.25)
        return False

    def resolve_component(self, package: str) -> str:
        """`pkg/cls` of the launcher activity, or "" when nothing declares one."""
        out = self.adb(
            "shell", "cmd", "package", "resolve-activity", "--brief",
            "-a", "android.intent.action.MAIN",
            "-c", "android.intent.category.LAUNCHER",
            package,
        )
        for line in reversed(out.splitlines()):
            line = line.strip()
            if "/" in line and not line.startswith(("priority", "No activity")):
                return line
        return ""

    def ensure_foreground(self, package: str, allow_cards: bool = True) -> str:
        """Bring `package` to the front by resuming its task, not starting it.

        Reordering a task to the front returns it with its state, which is what
        switching apps means. A task whose activity is not exported is refused
        by the system, so trying costs nothing; those fall back to a recents
        card. `allow_cards=False` drops that fallback, since cards carry labels
        rather than packages and a missed guess leaves the switcher covering
        the screen.
        """
        if not package or self.current_app()[0] == package:
            return ""
        for task in self.tasks():
            if task["pkg"] != package or not task["cls"]:
                continue
            self.adb(
                "shell",
                "am",
                "start",
                "--activity-reorder-to-front",
                "-n",
                f"{task['pkg']}/{task['cls']}",
            )
            if self.wait_front(package, 2.5):
                return f"switched to {package} (resumed its task)"
        if not allow_cards:
            return ""
        return self._foreground_by_card(package)

    def in_recents(self) -> bool:
        """Whether the task switcher is the screen in front.

        Launchers and OEM skins rename the activity, so this matches the family
        of markers they share. The exact `recents` marker is not enough: the
        platform class spells the word `RecentTasks`.
        """
        cls = self.current_app()[1].lower()
        markers = ("recenttask", "recents", "recent_activity", "recentactivity")
        return any(marker in cls for marker in markers)

    def _leave_recents(self) -> None:
        """Get the task switcher off the screen, whatever went wrong in it."""
        if self.in_recents():
            self.adb("shell", "input", "keyevent", "4")  # BACK closes the carousel
            self.wait_front_not("recents", 1.5)

    def wait_front_not(self, fragment: str, timeout: float = 2.0) -> bool:
        deadline = time.time() + timeout
        while time.time() < deadline:
            if fragment not in self.current_app()[1].lower():
                return True
            time.sleep(0.2)
        return False

    def _foreground_by_card(self, package: str) -> str:
        """Show a package's task by tapping recents cards until one is it.

        Cards report the app label, not the package, and the carousel only lays
        out the ones on screen, so the loop taps and verifies instead of
        matching a name, and swipes towards the older tasks when it runs out.

        Every exit closes the carousel: left open on failure, the switcher
        covers whatever the next call brings up.
        """
        tried: set[tuple[int, int]] = set()
        swipes = 0
        try:
            for _ in range(6):
                if not self.in_recents():
                    self.adb("shell", "input", "keyevent", "187")  # APP_SWITCH
                    time.sleep(0.5)
                untried = [c for c in self.cards() if c.center not in tried]
                if not untried:
                    if swipes >= 2:
                        break
                    swipes += 1
                    self.swipe_direction("right")  # older tasks sit further left
                    time.sleep(0.6)
                    continue
                card = untried[0]
                tried.add(card.center)
                self.tap(card)
                if self.wait_front(package, 2.5):
                    return f"switched to {package} (recents card {card.text!r})"
            return f"could not bring {package} to the front"
        finally:
            self._leave_recents()

    def cards(self, min_height: float = 0.3, min_width: float = 0.08) -> list[Element]:
        """The task cards of the recents screen, left to right.

        Size is what separates a card from the container, the per-card options
        button and the dock icons, so both limits are fractions of the screen
        and not pixels. The flag cannot be used either: a card is a labelled
        container around a nameless pane that takes the touch.
        """
        w, h = self.screen_size()
        els = self.elements()
        out: list[Element] = []
        seen: set[tuple[int, int, int, int]] = set()
        for e in els:
            if not e.listens or not e.text:
                continue
            box = e.area or e.bounds
            if box in seen:
                continue          # the same card, reached through its title
            if (box[3] - box[1]) < h * min_height or (box[2] - box[0]) < w * min_width:
                continue
            seen.add(box)
            if box == e.bounds:
                out.append(e)
            else:
                # Press the pane that reacts, at the spot its title occupies:
                # the pane is the whole window and can reach the status bar.
                out.append(
                    Element(0, e.text, e.kind, e.cls, "", box, True, False, hot=e.center)
                )
        return sorted(out, key=lambda e: (e.bounds[0], e.bounds[1]))


    @staticmethod
    def _inert_label(e: Element, els: list[Element]) -> bool:
        """Whether pressing this name would do nothing at all.

        A settings row is the shape this catches: its row is missing from the
        tree and what is left is a switch that ignores touches, so the name is
        the only way to reach the setting and nothing answers to a finger. A
        card title is the opposite — a window-sized pane behind it reacts — so
        the title is not the thing to press, it is the thing that names the
        thing to press.
        """
        return not e.listens and not e.editable

    def foreground(self, target: str) -> str:
        """Resume a background app by its recents card.

        `launch` restarts the app and throws away what it was doing, which is
        how a half-finished OAuth custom tab gets lost. The recents card
        resumes the exact task, and its title is usually the one the user read.
        """
        self.adb("shell", "input", "keyevent", "187")  # APP_SWITCH
        cards: list[Element] = []
        for _ in range(4):
            time.sleep(0.4)
            cards = self.cards()
            hit = next((c for c in cards if _target_matches(target, c.text)), None)
            if hit is not None:
                self.tap(hit)
                self.wait_idle(3.0)
                return f"ok foreground {target!r} -> {hit.text!r}"
        self.adb("shell", "input", "keyevent", "4")  # leave recents
        on_offer = ", ".join(repr(c.text) for c in cards[:6]) or "none"
        return f"no recents card matched {target!r}; cards on offer: {on_offer}"

    # ------------------------------------------------------------ diagnostics
    CRASH_HINTS = (
        "continúa fallando",
        "sigue fallando",
        "keeps stopping",
        "se ha detenido",
        "ha dejado de funcionar",
        "isn't responding",
        "no responde",
        "no responde.",
    )

    def showing_crash(self, elements: list[Element] | None = None) -> str:
        """The crash dialog text on screen, or '' when there is none."""
        els = elements if elements is not None else self.elements()
        blob = " | ".join(e.text.lower() for e in els if e.text)
        for hint in self.CRASH_HINTS:
            if hint in blob:
                return hint
        return ""

    def last_crash(self, package: str | None = None, limit: int = 4000) -> str:
        """Newest fatal exception from the crash buffer, in one compact line.

        The point is that the agent should not have to know that
        `logcat -b crash` exists, nor grep a 300-line stack trace by hand.
        """
        out = self.adb("shell", "logcat", "-d", "-b", "crash", "-t", str(limit))
        blocks = [b for b in (out or "").split("FATAL EXCEPTION")[1:]]
        if not blocks:
            return ""
        for block in reversed(blocks):
            info = self._parse_crash(block)
            if not info:
                continue
            if package and package not in info.get("process", ""):
                continue
            return self._format_crash(info)
        return ""

    LOG_PREFIX = re.compile(
        r"\d{2}-\d{2}\s+\d{2}:\d{2}:\d{2}\.\d+\s+\d+\s+\d+\s+[A-Z]\s+\S+:\s*"
    )

    def _parse_crash(self, block: str) -> dict:
        stamp = re.search(r"(\d{2}:\d{2}:\d{2})\.\d+", block)
        text = self.LOG_PREFIX.sub(" ", block)
        text = re.sub(r"\s+", " ", text).strip()
        proc = re.search(r"Process: ([\w.]+), PID: (\d+)", text)
        thrown = re.search(
            r"((?:[a-z][\w]*\.)+[A-Z]\w*(?:Exception|Error|Throwable)):? ([^@]*)", text
        )
        caused = re.search(r"Caused by: ([\w.$]+)(?:: (.+?))?(?= at |$)", text)
        frames = re.findall(r"at ([\w.$]+)\(([\w.]+:\d+)\)", text)
        if not proc and not thrown:
            return {}
        exc = caused.group(1) if caused else (thrown.group(1) if thrown else "?")
        msg = (caused.group(2) if caused and caused.group(2) else None) or (
            thrown.group(2).strip() if thrown and thrown.group(2) else ""
        )
        msg = re.sub(r"\s+", " ", msg).strip()
        if len(msg) > 160:
            msg = msg[:157] + "..."
        pkg = proc.group(1) if proc else ""
        own = [
            f"{cls.rsplit('.', 1)[-1]}({loc})"
            for cls, loc in frames
            if pkg and cls.startswith(pkg)
        ]
        return {
            "process": pkg,
            "exception": exc,
            "message": msg,
            "time": stamp.group(1) if stamp else "",
            "frames": own[:3] or [f"{c.rsplit('.', 1)[-1]}({l})" for c, l in frames[:3]],
        }

    def _format_crash(self, info: dict) -> str:
        frames = " <- ".join(info.get("frames") or [])
        when = f" [{info['time']}]" if info.get("time") else ""
        line = f"CRASH{when} {info.get('process', '?')} {info.get('exception', '?')}"
        if info.get("message"):
            line += f": {info['message']}"
        if frames:
            line += f" @ {frames}"
        return line

    def logs(self, pattern: str | None = None, lines: int = 40) -> str:
        """Recent logcat, optionally filtered. Keeps the agent out of adb grammar."""
        cmd = ["logcat", "-d", "-v", "brief", "-t", "400"]
        out = self.adb("shell", *cmd) or ""
        if pattern:
            needle = pattern.lower()
            kept = [ln for ln in out.splitlines() if needle in ln.lower()]
        else:
            kept = [ln for ln in out.splitlines() if " E " in ln or " W " in ln]
        return "\n".join(kept[-lines:]) or "(no matching log lines)"

    # ------------------------------------------------------------- one-call diag
    def package_errors(self, package: str, lines: int = 5) -> str:
        """Recent E/W lines that belong to `package` or to the crash machinery."""
        if not package or package == "?":
            return ""
        out = self.adb("shell", "logcat", "-d", "-v", "brief", "-t", "800") or ""
        short = package.rsplit(".", 1)[-1]
        kept = [
            ln
            for ln in out.splitlines()
            if (" E " in ln or " W " in ln)
            and (package in ln or short in ln or "AndroidRuntime" in ln)
        ]
        return "\n".join(ln[:220] for ln in kept[-lines:])

    def diag(self) -> str:
        """Everything that explains a broken screen, in one round trip.

        Built after a real bug: the agent had to chain five adb commands to
        learn that a settings screen was crashing, that the app was restored
        but stopped, and which package the missing provider belonged to.
        """
        pkg, act = self.current_app()
        on = self.screen_on()
        lines = [f"app={pkg} act={act} screen={'on' if on else 'OFF'}"]
        if not on:
            lines.append("display asleep; wake with `act key=wake`")
        else:
            w, h = self.screen_size()
            lines[0] += f" {w}x{h} kb={'on' if self.keyboard_visible() else 'off'}"
            if self.locked():
                lines.append("keyguard=LOCKED (input events will not pass)")
            over = self.overlay()
            if over:
                lines.append(f"overlay={over} (a dialog owns focus; act on it first)")
            dialog = self.showing_crash()
            if dialog:
                lines.append(f"crash dialog on screen: {dialog!r}")
        crash = self.last_crash()
        lines.append(crash or "crash buffer: clean")
        errs = self.package_errors(pkg)
        if errs:
            lines.append("recent errors for this app:\n" + errs)
        return "\n".join(lines)
