"""kaede-android-tools: three tools to drive an Android phone precisely.

Design rules:
  * every action returns the resulting screen, so the agent never needs a
    second call to find out what happened;
  * the same call works on native widgets and on web pages inside a browser;
  * coordinates are always derived from element bounds, never guessed.
"""

from __future__ import annotations

import os
import sys

from .device import Device, Element, config
from .web import Web

try:  # mcp >= 2 renamed FastMCP to MCPServer
    from mcp.server.mcpserver import MCPServer as _Server
except ImportError:  # pragma: no cover
    from mcp.server.fastmcp import FastMCP as _Server

mcp = _Server("kaede-android-tools")

_dev: Device | None = None
_web: Web | None = None


def device() -> Device:
    global _dev
    if _dev is None:
        _dev = Device(os.environ.get("ANDROID_SERIAL") or None)
    return _dev


def bridge() -> Web:
    global _web
    if _web is None:
        _web = Web(device())
    return _web


SHOT_PATH = "/tmp/kaede-android-screen.png"

# The width a picture of the screen is scaled down to before it is handed to
# the model. A 1080-wide PNG is ~2 MB and overflows the attachment budget, so
# the call that asked for a picture comes back with a file the model cannot
# open. Half width keeps labels readable and costs ~100 KB.
SHOT_WIDTH = 540

# Actions whose keystrokes land in whatever window is in front, so the page has
# to be in front before they run.
NEEDS_PAGE_IN_FRONT = ("type", "press", "fill", "click", "click_text")


def _spot(el: Element) -> str:
    x, y = el.hot or el.center
    return f"@{x},{y}"


def _near(a: tuple[int, int], b: tuple[int, int], slack: int = 40) -> bool:
    return abs(a[0] - b[0]) <= slack and abs(a[1] - b[1]) <= slack


def _inspect(dev: Device, target: str) -> str:
    """Every node a name points at, and whether pressing it can do anything.

    The same words on one screen are not the same thing: "Iniciar sesión" is
    a heading over the button that logs in, and a Label row runs to the width
    of the screen, so its middle is empty space. Both cases made the agent tap
    nothing and read the next screen as if the tap had worked. This answers
    the question before the tap instead of after it.
    """
    target = (target or "").strip()
    if not target:
        return "info needs target='text=Foo' | '#N' | 'x,y'"
    los = []
    if target.startswith("#"):
        el = dev._lookup(target)
        los = [el] if el else []
    else:
        point = target[1:] if target.startswith("@") else target
        if "," in point and point.replace(",", "").replace(" ", "").isdigit():
            x, y = (int(v) for v in point.split(","))
            els = dev.elements()
            # The label whose text was folded into a row has no line of its
            # own in the tree, so it is reconstructed: a label borrowed by a
            # clickable node keeps a spot inside it, and that spot is the only
            # record that the words are there at all.
            los = [e for e in els if e.contains((x, y))]
            for e in els:
                if e.hot and e.clickable and _near(e.hot, (x, y)):
                    los.append(
                        Element(0, e.text, "txt", "plain text", "", e.bounds, False, False, hot=e.hot)
                    )
            los.sort(key=lambda e: (e.bounds[2] - e.bounds[0]) * (e.bounds[3] - e.bounds[1]))
        else:
            needle = target.split("=", 1)[1] if "=" in target else target
            needle = needle.lstrip("~").lower()
            los = [e for e in dev.elements() if needle in e.text.lower()]
    if not los:
        return f"nothing on this screen matches {target!r}\n{_snapshot(False, False)}"
    lines = [f"{len(los)} node(s) match {target!r}"]
    for e in los:
        if e.editable:
            how = "a text field — type into it, there is nothing to press"
        elif e.listens and not e.clickable:
            how = "pressable through the control around it"
        elif e.clickable and e.hot:
            how = "pressable — but not at its centre, press the spot below"
        elif e.clickable:
            how = "pressable — the touch lands on it"
        else:
            how = "plain text — a tap here only does something if a control wraps it"
        lines.append(
            f"  {e.kind:<8} {e.text!r} {how}"
            f"\n           bounds={list(e.bounds)} centre={e.center}"
            + (f" label_at={e.hot}" if e.hot else "")
        )
    chosen = dev.resolve(target, want="tap")
    if chosen is not None:
        x, y = chosen.hot or chosen.center
        lines.append(
            f"tap {target!r} would press {chosen.kind} {chosen.text!r} at @{x},{y}"
        )
        others = [
            e
            for e in dev.elements()
            if e is not chosen
            and e.listens
            and e.text.strip()
            and chosen.text.startswith(e.text)
            and (
                (chosen.hot or chosen.center) != (e.hot or e.center)
                or chosen.bounds != e.bounds
            )
        ]
        if others:
            spots = ", ".join(_spot(e) for e in others[:3])
            lines.append(
                f"{len(others)} other node(s) here carry that name too ({spots});"
                " pick the exact @x,y when a name belongs to more than one control"
            )
    return "\n".join(lines)


def _snapshot(image: bool, full: bool, text_only: bool = False) -> str:
    out = device().snapshot(full=full, text_only=text_only)
    if image:
        try:
            shot = device().screenshot(SHOT_PATH, max_width=SHOT_WIDTH)
            out += f"\n[screenshot: {shot}]"
        except Exception as exc:  # a broken capture must never hide the tree
            out += f"\n[screenshot failed: {exc}]"
    return out


@mcp.tool(structured_output=False)
def see(image: bool = False, full: bool = False, text: bool = False) -> str:
    """Current screen as a compact, numbered list of interactive elements.

    Ref them as #N in act(target=...). text=true returns only the readable
    text, top to bottom, for reading a code out of a message.
    image=true writes a PNG to /tmp and returns its path; open it when the tree
    is not enough. full=true also lists labels that are not clickable.
    Rows keep their title and switch state, so `on`/`off` survives even when
    the row is the clickable node.
    An icon button can have no text, description or id: its row then shows
    `@x,y`, which is also a valid act target. Nameless refs are listed in the
    header; take an image to tell them apart instead of guessing.
    """
    return _snapshot(image, full, text)


@mcp.tool(structured_output=False)
def act(
    action: str,
    target: str | None = None,
    text: str | None = None,
    key: str | None = None,
    direction: str | None = None,
    timeout: float = 8.0,
    clear: bool = True,
    submit: bool = False,
    long: bool = False,
    image: bool = False,
) -> str:
    """Do one thing on the phone and return the resulting screen.

    action: tap | type | swipe | key | launch | foreground | open_url | wait |
            refresh | otp | passkey | devices | use | crash | logs | diag |
            shell | info
    target: '#3' | 'text=Foo' | '~foo' | 'id=pkg:id/name' | 'x,y'
    type uses text=, tap uses target=, key uses key= (back/home/enter/delete),
    swipe uses direction= (up/down/left/right) or target='x1,y1' text='x2,y2',
    launch uses text=<package>, foreground uses text=<recents card title, any
    target form> and resumes that task instead of restarting the package,
    open_url uses text=<url> and target='pkg=<package>' to skip the "open with"
    chooser, refresh reloads the current list or page,
    shell uses text=<command>.
    devices lists every attached phone with its model and which one is in use;
    use switches to one of them by serial or by the alias in
    ~/.config/kaede-android-tools/devices.json, reconnecting it over wireless
    debugging when the alias is a bare host.
    otp collects every code-shaped number from the notifications and the
    screen, newest first, with its source line; text= narrows it to one app or
    subject.
    passkey answers a "screen lock of your other device" prompt with text=<PIN>;
    it is a different secret from a mailed code, so otp never finds it. Without
    a PIN, tap the prompt's skip button instead.
    diag explains a broken screen in one call: app, display, keyguard, overlay,
    crash dialog, newest fatal exception, recent errors.
    crash prints the newest fatal exception only; logs greps logcat with text=.
    key also takes wake, sleep and power, which adb understands and
    uiautomator2 does not.
    info answers "is this a button or just text" for one label or point,
    listing every node with that name, its class and where to press; it presses
    nothing.
    """
    dev = device()
    action = action.lower().strip()

    # `launch target=com.pkg` means what `launch text=com.pkg` means; agents
    # reach for target= out of habit because tap uses it, and refusing the call
    # over which channel carried the string costs a round trip. The URL check
    # below keeps the two actions apart.
    package_hint = ""
    if action == "launch":
        text = text or target
    elif action == "open_url" and (target or "").startswith("pkg="):
        package_hint = target

    if action == "see":
        return _snapshot(image, False)

    if action in ("info", "find"):
        return _inspect(dev, target or "")

    if action in ("tap", "long"):
        el = dev.resolve(target or "", want="tap")
        if el is None:
            return f"not found: {target!r}\n{_snapshot(False, False)}"
        was = el.checked
        before = dev.raw_xml()
        if action == "long" or long:
            at = dev.long_press(el, 1200)
        else:
            at = dev.tap(el)
        dev.wait_idle()
        # A tap that changed nothing looks exactly like a tap that worked, and
        # the agent then reads the next screen as if it had moved. Comparing
        # the tree turns "ok" into a claim that can be checked.
        note = ""
        if dev.raw_xml() == before:
            note = (
                " — the screen did not change after the tap: that node may be"
                " plain text with nothing behind it, so check it landed before"
                " acting on the next screen"
            )
        # A toggle that just changed state is the whole point of the tap; say
        # it here instead of making the agent parse the new tree for it.
        if was is not None:
            now = dev.state_of(el.text)
            if now:
                note = f" -> {now}{note}"
        return f"ok {action} {el.text!r} @{at[0]},{at[1]}{note}\n{_snapshot(image, False)}"

    if action == "type":
        if text is None:
            return "type needs text="
        el = dev.resolve(target) if target else None
        if target and el is None:
            return f"not found: {target!r}\n{_snapshot(False, False)}"
        if el is not None:
            dev.tap(el)
            dev.wait_idle(1.5)
        # Aiming at a field is an instruction to type there; the app in front
        # stays.
        problem = _type(dev, text, clear, aimed=el is not None)
        if submit:
            _submit(dev)
        dev.wait_idle()
        note = f" ({problem})" if problem else ""
        return f"typed{note}\n{_snapshot(image, False)}"

    if action == "swipe":
        if target and text and "," in target and "," in text:
            x1, y1 = (int(v) for v in target.split(","))
            x2, y2 = (int(v) for v in text.split(","))
            dev.swipe_xy(x1, y1, x2, y2)
        else:
            dev.swipe_direction(direction or "up")
        dev.wait_idle()
        return f"ok swipe\n{_snapshot(image, False)}"

    if action == "refresh":
        how = dev.refresh()
        if how == "browser":
            _reload_browser()
        else:
            dev.wait_idle()
        return f"ok refresh\n{_snapshot(image, False)}"

    if action == "key":
        if not key:
            return "key needs key= (back/home/enter/delete/...)"
        dev.key(key)
        dev.wait_idle()
        return f"ok key {key}\n{_snapshot(image, False)}"

    if action == "launch":
        if not text:
            return "launch needs text=<package> (target= works too)"
        dev.launch(text)
        dev.wait_idle()
        return f"ok launch {text}\n{_snapshot(image, False)}"

    if action == "open_url":
        url = text or target or ""
        if not url.startswith(("http://", "https://")):
            return "open_url needs text=<url>"
        landed = dev.open_url(url, package=package_hint.removeprefix("pkg="))
        dev.wait_idle()
        return f"ok open_url -> {landed}\n{_snapshot(image, False)}"

    if action == "wait":
        if dev.wait_for(target or "", timeout):
            return f"appeared: {target}\n{_snapshot(image, False)}"
        return f"timeout waiting for {target!r}\n{_snapshot(image, False)}"

    if action == "otp":
        return dev.otp(text)

    if action == "passkey":
        if not text:
            return (
                "passkey needs text=<the other device's screen lock PIN>; "
                "use action=otp for a mailed verification code"
            )
        return dev.passkey(text)

    if action == "devices":
        return dev.device_list()

    if action == "use":
        return dev.use(text or "")

    if action == "foreground":
        if not text:
            return "foreground needs text=<recents card title, as a target>"
        note = dev.foreground(text)
        return f"{note}\n{_snapshot(image, False)}"

    if action == "crash":
        info = dev.last_crash(text)
        return info or "no fatal exception in the crash buffer"

    if action == "diag":
        return dev.diag()

    if action == "logs":
        return dev.logs(text, int(timeout) if timeout > 8 else 40)

    if action == "shell":
        if not text:
            return "shell needs text=<command>"
        return dev.adb("shell", text, timeout=int(max(timeout, 30)))

    return f"unknown action {action!r}"


@mcp.tool(structured_output=False)
def web(
    action: str,
    selector: str | None = None,
    script: str | None = None,
    text: str | None = None,
    url: str | None = None,
    page: str | None = None,
    key: str | None = None,
) -> str:
    """The browser side of the phone, through the DevTools protocol.

    action: pages | text | eval | fill | click | click_text | press | type |
            reload | open
    pages lists tabs (title, url, page id) across every browser process.
    text lists the visible text of the page; eval runs JS with script=.
    fill needs selector= and text=; click needs selector=; click_text clicks
        the element whose visible text matches text=; press sends key= (enter,
        tab, escape, backspace); type writes text= into the focused field and
        says so when the tab does not own keyboard focus (a backgrounded tab
        drops CDP keystrokes silently, so bring it to the front first);
        reload reloads the page; open needs url=.
    """
    bridge_ = bridge()
    action = action.lower().strip()
    switched = ""
    if action in NEEDS_PAGE_IN_FRONT:
        usable, note = _bring_page_front(device())
        if not usable:
            return note
        switched = f"{note}; " if note else ""

    if action == "pages":
        pages = bridge_.pages()
        if not pages:
            return "no debuggable page (open a browser tab on the phone)"
        return "\n".join(f"[{p['key']}] {p['title']!r} {p['url']}" for p in pages)

    if action == "text":
        return "\n".join(bridge_.visible_text(page)) or "(no text)"

    if action == "click_text":
        if not text:
            return "click_text needs text="
        ok = bridge_.click_text(text, page)
        return f"{switched}click_text {text!r}: {'ok' if ok else 'not found'}"

    if action == "press":
        if not key:
            return "press needs key="
        bridge_.press(key, page)
        return f"{switched}pressed {key}"

    if action == "type":
        if text is None:
            return "type needs text="
        before = {p["key"]: p["url"] for p in bridge_.pages()}
        ok = bridge_.type_text(text, page)
        if ok:
            return f"{switched}typed: ok {text!r}"
        gone = [k for k in before if k not in {p["key"] for p in bridge_.pages(fresh=True)}]
        if gone or any(p["url"] != before.get(p["key"]) for p in bridge_.pages(fresh=True)):
            # The page took the keystrokes and navigated on. Reading the fields
            # back after a client-side redirect is how a successful submit gets
            # reported as a dead field.
            return f"{switched}typed: ok (page navigated after the last key)"
        if not bridge_.any_page_focused():
            return (
                f"{switched}typed: nothing, the page does not own keyboard focus — the "
                "browser is in the background, so CDP drops the key events. "
                "Run act foreground with the card title first, then retry."
            )
        return f"{switched}typed: no effect (the field ignored real keystrokes)"

    if action == "reload":
        bridge_.reload(page)
        return "reloaded"

    if action == "eval":
        if not script:
            return "eval needs script="
        import json as _json

        return _json.dumps(bridge_.ev(script, page), ensure_ascii=False)[:4000]

    if action == "fill":
        if not selector or text is None:
            return "fill needs selector= and text="
        return f"{switched}filled {selector} -> {bridge_.fill(selector, text, page)!r}"

    if action == "click":
        if not selector:
            return "click needs selector="
        return f"{switched}click {selector}: {'ok' if bridge_.click(selector, page) else 'not found'}"

    if action == "open":
        if not url:
            return "open needs url="
        device().open_url(url)
        device().wait_idle()
        return f"opened {url}"

    return f"unknown action {action!r}"


# --------------------------------------------------------------------- typing
# What a password field shows instead of what was typed. Every Android IME and
# widget uses one of these, and a read-back that compares the literal text
# against them calls a successful write a failure.
MASK_CHARS = "\u2022*\u00b7\u25cf\u25cb\u2219"


def _hold(shown: str, wanted: str) -> bool:
    """Whether `shown` is `wanted`, allowing for a field that masks it.

    A password field is verified by length and masking only: there is no way
    to read the characters back, and pretending otherwise turns the one honest
    signal available into a false failure.
    """
    if not wanted or not shown:
        return False
    if wanted in shown:
        return True
    probe = shown.strip()
    return (
        bool(probe)
        and len(probe) == len(wanted)
        and all(ch in MASK_CHARS for ch in probe)
    )


def _native_write(dev: Device, text: str, clear: bool) -> bool:
    """Type through the phone's own input pipeline. True when it landed."""
    if clear:
        current = dev.focused_text()
        if current:
            dev.backspace(len(current) + 2)
    try:
        # Real key events at the input-device level: the one route a WebView
        # field actually honours.
        dev.input_text(text)
        dev.wait_idle(1.0)
    except Exception:
        pass
    if _hold(dev.focused_text(), text) or _web_holds(text):
        return True
    try:
        d = dev.d()
        field = d(className="android.widget.EditText", focused=True)
        if not field.exists:
            field = d(className="android.widget.EditText")
        if field.exists:
            field.send_keys(text)
            dev.wait_idle(1.0)
    except Exception:
        pass
    return _hold(dev.focused_text(), text) or _web_holds(text)


def _web_holds(needle: str) -> bool:
    """Whether any open page's editable fields, joined, hold `needle`.

    The fields are joined because an OTP form spreads the value over several
    inputs and clears the one in focus after every keystroke, so reading the
    focused element alone reports a successful write as a failure.
    """
    br = bridge()
    try:
        pages = br.pages()
    except Exception:
        return False
    for page in pages:
        try:
            if br.holds(needle, page["key"]):
                return True
        except Exception:
            continue
    return False


def _cdp_write(text: str, clear: bool = True) -> bool:
    """Write into the web field on whichever page owns focus."""
    br = bridge()
    try:
        before = {p["key"]: p["url"] for p in br.pages()}
    except Exception:
        return False
    for key, url in before.items():
        try:
            br.fill_focused(text, key, clear)
        except Exception:
            # A page that no longer exists is a page that navigated away after
            # accepting the write. That is a login succeeding, not a failure.
            try:
                alive = {p["key"] for p in br.pages(fresh=True)}
            except Exception:
                return True
            if key not in alive:
                return True
            continue
        try:
            if br.holds(text, key):
                return True
        except Exception:
            # The document is gone because the write was accepted and the page
            # navigated on. Reporting a failure here is how a successful login
            # gets called a broken field.
            return True
        try:
            after = {p["key"]: p["url"] for p in br.pages(fresh=True)}
        except Exception:
            return True
        if after.get(key) != url:
            return True
    return False


def _submit(dev: Device) -> None:
    """Get the form to accept what was typed.

    The IME enter of a WebView never runs a form's implicit submit, so a
    browser needs the trusted key event instead — and even in a browser, enter
    only works when the page wires it up. The configured submit selectors come
    first because they are the ones the caller can correct without a rebuild,
    and a page that names its own button is a page enter does not always submit.
    """
    if dev.is_browser(dev.current_app()[0]):
        br = bridge()
        selectors = config().get("submit_selectors") or []
        if isinstance(selectors, str):
            selectors = [selectors]
        joined = ", ".join(str(s).strip() for s in selectors if str(s).strip())
        if joined:
            try:
                if br.click_real(joined):
                    return
            except Exception:
                pass
        try:
            br.press("enter")
            return
        except Exception:
            pass
    dev.key("enter")


def _page_owns_focus() -> bool | None:
    """Whether a real page holds keyboard focus. None when there is no telling.

    Only a page that exists and reports no focus proves the browser is in the
    background; no pages at all proves nothing, and guessing there would turn
    an unknown into a wrong diagnosis.
    """
    try:
        br = bridge()
        pages = br.pages()
    except Exception:
        return None
    if not pages:
        return None
    try:
        return br.any_page_focused()
    except Exception:
        return None


def _page_owner(dev: Device) -> str:
    """The browser package that owns the phone's debuggable page."""
    try:
        if not bridge().pages():
            return ""
    except Exception:
        return ""
    try:
        tasks = dev.tasks()
    except Exception:
        return ""
    for task in tasks:
        if dev.is_browser(task["pkg"]):
            return task["pkg"]
    return ""


def _typed_field_is_here(dev: Device) -> bool:
    """Whether the app in front is already asking for text.

    A focused, editable node on the screen in front is where Android will send
    the keystrokes, whatever else is running. That makes it the destination by
    definition: no page, task or owner has a vote over it.
    """
    try:
        return any(el.editable and el.focused for el in dev.elements())
    except Exception:
        return False


def _bring_page_front(dev: Device) -> tuple[bool, str]:
    """Put the app that owns the web page in front. Returns (usable, note).

    A keystroke cannot be aimed at a window that is not in front, so this is
    the difference between typing and typing into the wrong app.

    The order is the design:

      1. An app in front with a focused field wins. It is what the person sees
         and what holds the caret; switching away throws away the field and
         everything typed around it. A tab open behind another app is not that
         field's owner.
      2. Only when nothing in front asks for text does the page get a say, and
         only if a browser task really owns it.
      3. Otherwise refuse and say what is on screen.

    Step 1 runs first because "a browser task exists" is true in nearly every
    session and says nothing about where the keys belong.
    """
    if _typed_field_is_here(dev):
        return True, ""
    try:
        pages = bridge().pages()
    except Exception:
        return True, ""
    shown = dev.current_app()[0]
    if not pages or dev.is_browser(shown):
        return True, ""
    owner = _page_owner(dev)
    if owner:
        note = dev.ensure_foreground(owner)
        if dev.wait_front(owner, 3.0):
            return True, note
    where = pages[0].get("url") or pages[0].get("title") or pages[0].get("key")
    return False, (
        f"nothing typed: nothing on screen is asking for text and the page "
        f"behind it ({where}) is not the app in front. Open that page or tap "
        "the field first"
    )


def _wrong_app(dev: Device) -> str:
    """Kept for callers that only want to know, without the switch."""
    usable, note = _bring_page_front(dev)
    return "" if usable else note


def _type(dev: Device, text: str, clear: bool, aimed: bool = False) -> str:
    """Write where focus is, verify it landed, escalate to the other layer.

    `aimed` means the caller already tapped a field, so the screen in front is
    the destination by definition. Switching apps then throws away the field
    that was pointed at: a native web-view host has no CDP endpoint, so a page
    open in the background is not it.
    """
    if aimed:
        usable, note = True, ""
    else:
        usable, note = _bring_page_front(dev)
    if not usable:
        return note
    prefix = f"{note}\n" if note else ""
    if dev.is_browser(dev.current_app()[0]):
        if _cdp_write(text, clear) or _native_write(dev, text, clear):
            return prefix
        if _cdp_write(text, clear):
            return prefix
        # CDP accepts keystrokes for a backgrounded tab and drops them, so
        # "no effect" is a wrong answer when the page never had focus.
        if _page_owns_focus() is False:
            return prefix + (
                "nothing typed: the tab is open but does not own the keyboard, "
                "so CDP accepts the keys and drops them — open that tab first"
            )
        return prefix + "write did not register (no focused field?)"
    if _native_write(dev, text, clear):
        return prefix
    if _cdp_write(text, clear) or _web_holds(text):
        return prefix
    if not _typed_field_is_here(dev):
        # No field in front and no page that took the text: the keystrokes had
        # nowhere to go, so say that instead of blaming a missing field that
        # was never there. This is the same shape on every app.
        return prefix + (
            "nothing typed: nothing in front is asking for text — tap the field "
            "first, or pass target= with the field's ref"
        )
    return prefix + "write did not register (no focused field?)"


def _reload_browser() -> None:
    br = bridge()
    try:
        pages = br.pages()
    except Exception:
        return
    for p in pages:
        try:
            br.reload(p["key"])
        except Exception:
            continue


def main() -> None:
    import logging

    logging.basicConfig(stream=sys.stderr, level=logging.WARNING)
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
