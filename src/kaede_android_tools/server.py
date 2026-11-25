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

from .device import Device
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


def _snapshot(image: bool, full: bool) -> str:
    out = device().snapshot(full=full)
    if image:
        out += "\n[screenshot attached]"
    return out


@mcp.tool()
def see(image: bool = False, full: bool = False) -> str:
    """Current screen as a compact, numbered list of interactive elements.

    Ref them as #N in act(target=...). image=true also returns a screenshot.
    """
    return _snapshot(image, full)


@mcp.tool()
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

    action: tap | type | swipe | key | launch | open_url | wait | shell
    target: '#3' | 'text=Foo' | '~foo' | 'id=pkg:id/name' | 'x,y'
    type uses text=, tap uses target=, key uses key= (back/home/enter/delete),
    swipe uses direction= (up/down/left/right), launch uses text=<package>,
    open_url uses text=<url>, shell uses text=<command>.
    """
    dev = device()
    action = action.lower().strip()

    if action == "see":
        return _snapshot(image, False)

    if action in ("tap", "long"):
        el = dev.resolve(target or "")
        if el is None:
            return f"not found: {target!r}\n{_snapshot(False, False)}"
        dev.long_press(el, 1200) if (action == "long" or long) else dev.tap(el)
        dev.wait_idle()
        return f"ok {action} {el.line()}\n{_snapshot(image, False)}"

    if action == "type":
        if text is None:
            return "type needs text="
        el = dev.resolve(target) if target else None
        if target and el is None:
            return f"not found: {target!r}\n{_snapshot(False, False)}"
        if el is not None:
            dev.tap(el)
            dev.wait_idle(1.5)
        problem = _type(dev, text, clear)
        if submit:
            dev.key("enter")
        dev.wait_idle()
        note = f" ({problem})" if problem else ""
        return f"typed{note}\n{_snapshot(image, False)}"

    if action == "swipe":
        dev.swipe_direction(direction or "up")
        dev.wait_idle()
        return f"ok swipe {direction or 'up'}\n{_snapshot(image, False)}"

    if action == "key":
        if not key:
            return "key needs key= (back/home/enter/delete/...)"
        dev.key(key)
        dev.wait_idle()
        return f"ok key {key}\n{_snapshot(image, False)}"

    if action == "launch":
        if not text:
            return "launch needs text=<package>"
        dev.launch(text)
        dev.wait_idle()
        return f"ok launch {text}\n{_snapshot(image, False)}"

    if action == "open_url":
        if not text:
            return "open_url needs text=<url>"
        dev.open_url(text)
        dev.wait_idle()
        return f"ok open_url\n{_snapshot(image, False)}"

    if action == "wait":
        if dev.wait_for(target or "", timeout):
            return f"appeared: {target}\n{_snapshot(image, False)}"
        return f"timeout waiting for {target!r}\n{_snapshot(image, False)}"

    if action == "shell":
        if not text:
            return "shell needs text=<command>"
        return dev.adb("shell", text, timeout=int(max(timeout, 30)))

    return f"unknown action {action!r}"


@mcp.tool()
def web(
    action: str,
    selector: str | None = None,
    script: str | None = None,
    text: str | None = None,
    url: str | None = None,
    page: str | None = None,
) -> str:
    """The browser side of the phone, through Chrome DevTools.

    action: pages | eval | fill | click | open
    pages lists tabs (title, url, page id) across every Chrome process.
    eval runs JS with script=; fill needs selector= and text=; click needs
    selector=; open needs url= and loads it in the phone's browser.
    """
    bridge_ = bridge()
    action = action.lower().strip()

    if action == "pages":
        pages = bridge_.pages()
        if not pages:
            return "no debuggable page (open a browser tab on the phone)"
        return "\n".join(f"[{p['key']}] {p['title']!r} {p['url']}" for p in pages)

    if action == "eval":
        if not script:
            return "eval needs script="
        import json as _json

        return _json.dumps(bridge_.ev(script, page), ensure_ascii=False)[:4000]

    if action == "fill":
        if not selector or text is None:
            return "fill needs selector= and text="
        return f"filled {selector} -> {bridge_.fill(selector, text, page)!r}"

    if action == "click":
        if not selector:
            return "click needs selector="
        return f"click {selector}: {'ok' if bridge_.click(selector, page) else 'not found'}"

    if action == "open":
        if not url:
            return "open needs url="
        device().open_url(url)
        device().wait_idle()
        return f"opened {url}"

    return f"unknown action {action!r}"


# --------------------------------------------------------------------- typing
def _native_write(dev: Device, text: str, clear: bool) -> None:
    """Try the accessibility path first, then raw key input."""
    d = dev.d()
    try:
        field = d(className="android.widget.EditText", focused=True)
        if not field.exists:
            field = d(className="android.widget.EditText")
        if field.exists:
            field.set_text(text) if clear else field.send_keys(text)
            dev.wait_idle(1.5)
            return
    except Exception:
        pass
    try:
        if clear:
            dev.adb(
                "shell",
                "input keyevent 123 && input keycombination 113 29 && input keyevent 67",
            )
        d.send_keys(text)
        dev.wait_idle(1.5)
    except Exception:
        pass


def _web_pages_with_focus(needle: str) -> list[dict]:
    """Pages whose focused element now holds `needle`, or is at least editable."""
    out: list[dict] = []
    br = bridge()
    try:
        pages = br.pages()
    except Exception:
        return out
    for page in pages:
        try:
            value = br.ev(
                "(() => { const e = document.activeElement;"
                " return e ? String(e.value ?? '') : ''; })()",
                page["key"],
            )
            if needle and needle in (value or ""):
                out.append(page)
        except Exception:
            continue
    return out


def _cdp_write(text: str) -> bool:
    """Write into the web field on whichever page owns focus."""
    br = bridge()
    try:
        pages = br.pages()
    except Exception:
        return False
    for page in pages:
        try:
            out = br.fill_focused(text, page["key"])
        except Exception:
            continue
        if out.get("ok") and text in str(out.get("value") or ""):
            return True
    return False


def _type(dev: Device, text: str, clear: bool) -> str:
    """Write where focus is, verify it landed, escalate to the other layer."""
    browserish = dev.is_browser(dev.current_app()[0])
    if browserish and _cdp_write(text):
        return ""
    _native_write(dev, text, clear and not browserish)
    if text in dev.focused_text() or _web_pages_with_focus(text):
        return ""
    if not browserish and _cdp_write(text):
        return ""
    if _web_pages_with_focus(text):
        return ""
    return "write did not register (no focused field?)"


def main() -> None:
    import logging

    logging.basicConfig(stream=sys.stderr, level=logging.WARNING)
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
