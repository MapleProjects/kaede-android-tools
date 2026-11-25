"""Chrome DevTools layer: discovers the phone's debug sockets, forwards them and
speaks CDP. No port juggling for the caller."""

from __future__ import annotations

import json
import time
import re
import urllib.request

SOCKET_RE = re.compile(r"@([A-Za-z0-9_.:-]*devtools_remote[A-Za-z0-9_.:-]*)")
BASE_PORT = 9400


class Web:
    def __init__(self, device) -> None:
        self.device = device
        self._forwarded: dict[str, int] = {}
        self._next_port = BASE_PORT
        self._cache: tuple[float, list[dict]] | None = None

    # ------------------------------------------------------------ discovery
    def sockets(self) -> list[str]:
        out = self.device.adb("shell", "cat", "/proc/net/unix")
        return sorted(set(SOCKET_RE.findall(out or "")))

    def _probe(self, port: int) -> bool:
        try:
            with urllib.request.urlopen(
                f"http://127.0.0.1:{port}/json/version", timeout=2
            ) as resp:
                return resp.status == 200
        except Exception:
            return False

    def _forward(self, name: str) -> int | None:
        cached = self._forwarded.get(name)
        if cached is not None:
            if self._probe(cached):
                return cached
            self._forget(name)
        for _ in range(8):
            port = self._next_port
            self._next_port += 1
            self.device.adb("forward", "--remove", f"tcp:{port}")
            self.device.adb("forward", f"tcp:{port}", f"localabstract:{name}")
            # adb prints the port on success but nothing when it merely rebinds
            # an existing rule, so trust a real HTTP probe instead of stdout.
            if self._probe(port):
                self._forwarded[name] = port
                return port
            self.device.adb("forward", "--remove", f"tcp:{port}")
        return None

    def _forget(self, name: str) -> None:
        port = self._forwarded.pop(name, None)
        if port is not None:
            self.device.adb("forward", "--remove", f"tcp:{port}")

    def pages(self, fresh: bool = False, ttl: float = 1.5) -> list[dict]:
        now = time.time()
        if not fresh and self._cache and now - self._cache[0] < ttl:
            return self._cache[1]
        found: list[dict] = []
        for name in self.sockets():
            port = self._forward(name)
            if port is None:
                continue
            try:
                with urllib.request.urlopen(
                    f"http://127.0.0.1:{port}/json/list", timeout=3
                ) as resp:
                    data = json.load(resp)
            except Exception:
                continue
            for page in data:
                if page.get("type") != "page":
                    continue
                found.append(
                    {
                        "socket": name,
                        "port": port,
                        # ids repeat across Chrome processes, so namespace them
                        "key": f"{port}:{page.get('id')}",
                        "id": page.get("id"),
                        "title": (page.get("title") or "")[:80],
                        "url": (page.get("url") or "")[:160],
                        "ws": page.get("webSocketDebuggerUrl", ""),
                    }
                )
        self._cache = (now, found)
        return found

    def _pick(self, page_id: str | None) -> dict | None:
        pages = self.pages()
        if not pages:
            return None
        if page_id is None:
            # Prefer a real document over chrome-native:// placeholder tabs,
            # and among those the tab that owns the keyboard: with no page=
            # given the caller means the one on screen, and the list order is
            # process order, not what the phone is showing.
            web_pages = [p for p in pages if p["url"].startswith("http")]
            pool = web_pages or pages
            for page in pool:
                try:
                    if self.ev("document.hasFocus()", page["key"]):
                        return page
                except Exception:
                    continue
            return pool[0]
        for page in pages:
            if page["key"] == page_id:
                return page
        # A page migrates to another process (and therefore another port) after
        # a navigation that swaps renderers, so retry on the bare id too.
        for page in pages:
            if page["id"] == page_id or page["url"].startswith(page_id):
                return page
        keys = ", ".join(p["key"] for p in pages)
        raise RuntimeError(f"page {page_id!r} is gone; available pages: {keys}")

    # ----------------------------------------------------------------- cdp
    def _ws(self, page: dict):
        import websocket

        return websocket.create_connection(page["ws"], timeout=15, suppress_origin=True)

    def call(self, method: str, params: dict | None = None, page_id: str | None = None):
        page = self._pick(page_id)
        if page is None:
            raise RuntimeError(
                "no debuggable page: open a browser tab on the phone first"
            )
        ws = self._ws(page)
        try:
            ws.send(json.dumps({"id": 1, "method": method, "params": params or {}}))
            while True:
                msg = json.loads(ws.recv())
                if msg.get("id") == 1:
                    if "error" in msg:
                        raise RuntimeError(msg["error"].get("message", "cdp error"))
                    return msg.get("result", {})
        finally:
            ws.close()

    def ev(self, script: str, page_id: str | None = None):
        res = self.call(
            "Runtime.evaluate",
            {"expression": script, "returnByValue": True, "awaitPromise": True},
            page_id,
        )
        result = res.get("result", {})
        if result.get("subtype") == "error":
            raise RuntimeError(result.get("description", "js error"))
        return result.get("value")

    # -------------------------------------------------------------- helpers
    # Reading back only the focused element is a lie on any form that spreads
    # the value over several fields: an OTP page keeps one digit per input and
    # moves focus after each keystroke, so the box in focus is empty again by
    # the time a caller looks. Every check below joins all the visible fields.
    FIELDS_JS = (
        "Array.from(document.querySelectorAll("
        "  'input:not([type=hidden]):not([type=submit]):not([type=button]),"
        "   textarea,[contenteditable=true]'))"
        "  .filter((e) => e.offsetParent !== null && !e.disabled)"
    )

    def field_values(self, page_id: str | None = None) -> list[str]:
        script = f"(() => {{ return {self.FIELDS_JS}.map((e) => String(e.value ?? e.textContent ?? '')); }})()"
        return list(self.ev(script, page_id) or [])

    def holds(self, needle: str, page_id: str | None = None) -> bool:
        """Whether the page's editable fields, taken together, hold `needle`."""
        if not needle:
            return False
        values = self.field_values(page_id)
        return needle in "".join(values) or any(needle in v for v in values)

    def fill(self, selector: str, text: str, page_id: str | None = None) -> str:
        focus = (
            "(() => {"
            f"const el = document.querySelector({json.dumps(selector)});"
            "if (!el) return false;"
            "el.scrollIntoView({block:'center'});"
            "el.focus();"
            "return true;"
            "})()"
        )
        if not self.ev(focus, page_id):
            raise RuntimeError("selector matched nothing")
        # Keystrokes are what a person produces and what every auth form
        # expects. Assigning the whole string at once satisfies a naive
        # read-back while leaving an OTP form with its other boxes empty.
        self._send_chars(text, page_id)
        if self.holds(text, page_id):
            return "".join(self.field_values(page_id))
        return self.assign(text, page_id, selector)

    def assign(self, text: str, page_id: str | None = None, selector: str | None = None):
        """Last resort: set .value directly and fire input/change."""
        target = (
            f"document.querySelector({json.dumps(selector)})"
            if selector
            else "document.activeElement"
        )
        script = (
            "(() => {"
            f"const el = {target};"
            "if (!el) return {ok:false, value:null};"
            "if (el.isContentEditable) { el.textContent = "
            + json.dumps(text)
            + "; } else {"
            "  const proto = el.tagName === 'TEXTAREA'"
            "    ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype;"
            "  Object.getOwnPropertyDescriptor(proto, 'value').set.call(el, "
            + json.dumps(text)
            + ");"
            "}"
            "el.dispatchEvent(new Event('input', {bubbles:true}));"
            "el.dispatchEvent(new Event('change', {bubbles:true}));"
            "return {ok:true, value: el.value ?? el.textContent ?? ''};"
            "})()"
        )
        out = self.ev(script, page_id) or {"ok": False, "value": None}
        return out.get("value") if out.get("ok") else None

    def click(self, selector: str, page_id: str | None = None) -> bool:
        script = (
            "(() => {"
            f"const el = document.querySelector({json.dumps(selector)});"
            "if (!el) return false;"
            "el.scrollIntoView({block:'center'});"
            "el.click();"
            "return true;"
            "})()"
        )
        return bool(self.ev(script, page_id))

    def insert_text(self, text: str, page_id: str | None = None) -> None:
        self.call("Input.insertText", {"text": text}, page_id)

    def press(self, key: str, page_id: str | None = None) -> None:
        """A trusted key press. Synthetic KeyboardEvents are ignored by most
        auth flows; these go through the browser input pipeline."""
        codes = {
            "enter": ("Enter", "Enter", 13),
            "tab": ("Tab", "Tab", 9),
            "escape": ("Escape", "Escape", 27),
            "backspace": ("Backspace", "Backspace", 8),
            "arrowdown": ("ArrowDown", "ArrowDown", 40),
            "arrowup": ("ArrowUp", "ArrowUp", 38),
        }
        name = key.strip().lower()
        if name not in codes:
            raise RuntimeError(f"unsupported key {key!r}; use {sorted(codes)}")
        k, code, vk = codes[name]
        for kind in ("rawKeyDown", "char", "keyUp"):
            params = {
                "type": kind,
                "key": k,
                "code": code,
                "windowsVirtualKeyCode": vk,
                "nativeVirtualKeyCode": vk,
            }
            if kind == "char" and k != "Enter":
                params.update({"text": "\t" if k == "Tab" else ""})
            if kind == "rawKeyDown" and k == "Enter":
                # Without the carriage return the browser never runs the
                # implicit submit of a form, so "press enter" silently no-ops.
                params["text"] = "\r"
                params["unmodifiedText"] = "\r"
            self.call("Input.dispatchKeyEvent", params, page_id)

    def _rect_of(self, js_find: str, page_id: str | None = None) -> dict | None:
        script = (
            "(() => {"
            f"{js_find}"
            "if (!el) return null;"
            "el.scrollIntoView({block:'center', inline:'center'});"
            "const r = el.getBoundingClientRect();"
            "if (r.width === 0 || r.height === 0) return null;"
            "return {x: r.x + r.width/2, y: r.y + r.height/2};"
            "})()"
        )
        return self.ev(script, page_id)

    def click_real(self, selector: str, page_id: str | None = None) -> bool:
        """Click with real mouse events. el.click() is not trusted, so SPAs and
        auth pages that check isTrusted will silently ignore it."""
        find = f"const el = document.querySelector({json.dumps(selector)});"
        point = self._rect_of(find, page_id)
        if not point:
            return False
        return self._click_point(point, page_id)

    def click_text(self, text: str, page_id: str | None = None) -> bool:
        """Click the element whose visible text matches, no CSS needed."""
        find = (
            "const norm = (s) => (s || '').replace(/\\s+/g, ' ').trim();"
            f"const want = norm({json.dumps(text)});"
            "const cands = [...document.querySelectorAll("
            "  'button,a,[role=button],[role=link],span,div,input[type=submit]')]"
            "  .filter(e => e.offsetParent !== null && norm(e.innerText || e.value) === want);"
            "const el = cands[cands.length - 1];"
        )
        point = self._rect_of(find, page_id)
        if not point:
            return False
        return self._click_point(point, page_id)

    def _click_point(self, point: dict, page_id: str | None = None) -> bool:
        base = {
            "x": point["x"],
            "y": point["y"],
            "button": "left",
            "clickCount": 1,
            "buttons": 1,
        }
        self.call("Input.dispatchMouseEvent", {"type": "mouseMoved", **base}, page_id)
        self.call("Input.dispatchMouseEvent", {"type": "mousePressed", **base}, page_id)
        self.call("Input.dispatchMouseEvent", {"type": "mouseReleased", **base}, page_id)
        return True

    def visible_text(self, page_id: str | None = None, limit: int = 80) -> list[str]:
        script = (
            "(() => { const seen = new Set(); const out = [];"
            "for (const e of document.querySelectorAll("
            "  'body *')) { if (e.offsetParent === null) continue;"
            "  if (e.children.length) continue;"
            "  const t = (e.innerText || '').trim();"
            "  if (t && !seen.has(t)) { seen.add(t); out.push(t); }"
            f"  if (out.length >= {limit}) break; }}"
            "return out; })()"
        )
        return self.ev(script, page_id) or []

    def reload(self, page_id: str | None = None) -> None:
        try:
            self.call("Page.reload", {"ignoreCache": True}, page_id)
        except Exception:
            self.ev("location.reload(); 1", page_id)

    FOCUS_JS = (
        "(() => { const ok = (e) => !!e && (e.tagName === 'INPUT'"
        "  || e.tagName === 'TEXTAREA' || e.isContentEditable);"
        "let el = document.activeElement;"
        "if (!ok(el)) { el = [...document.querySelectorAll("
        "  'input:not([type=hidden]),textarea,[contenteditable=true]')]"
        "  .find(e => e.offsetParent !== null && !e.disabled && !e.readOnly); }"
        "if (el) { el.focus(); return 1; } return 0; })()"
    )

    def _typing_targets(self, page_id: str | None) -> list[dict]:
        """Pages to try, in the order a person would.

        Two tabs in different renderer processes can both answer
        `document.hasFocus()` with true, and the one that does is not
        necessarily the one with a field — so the caller gets an order, not an
        answer, and the write is verified page by page.
        """
        if page_id is not None:
            page = self._pick(page_id)
            return [page] if page else []
        pages = [p for p in self.pages() if p["url"].startswith("http")]
        keyed = [p for p in pages if self.page_focused(p["key"])]
        return keyed + [p for p in pages if p not in keyed]

    def type_text(self, text: str, page_id: str | None = None, clear: bool = False) -> bool:
        """Focus the first usable field and type it like a person would.

        Real key events matter: one-shot value assignment leaves OTP boxes
        empty because their auto-advance handlers never fire.
        """
        for page in self._typing_targets(page_id):
            key = page["key"]
            if not self.ev(self.FOCUS_JS, key):
                continue
            if clear:
                self.clear_focused(key)
            self._send_chars(text, key)
            if self.holds(text, key):
                return True
        return False

    def clear_focused(self, page_id: str | None = None) -> None:
        """Select all and delete, through the same trusted input path."""
        for kind in ("rawKeyDown", "keyUp"):
            self.call(
                "Input.dispatchKeyEvent",
                {
                    "type": kind,
                    "key": "a",
                    "code": "KeyA",
                    "windowsVirtualKeyCode": 65,
                    "nativeVirtualKeyCode": 65,
                    "modifiers": 2,
                },
                page_id,
            )
        self.press("backspace", page_id)

    def _send_chars(self, text: str, page_id: str | None = None) -> None:
        """One trusted key event per character, never a bulk insert."""
        for ch in text:
            vk = ord(ch.upper()) if ch.isalnum() else 32 if ch == " " else None
            if vk is None:
                # Punctuation has no portable virtual-key code; insertText is
                # the only way to get the character itself into the pipeline.
                self.call("Input.insertText", {"text": ch}, page_id)
                continue
            base = {"windowsVirtualKeyCode": vk, "nativeVirtualKeyCode": vk}
            self.call(
                "Input.dispatchKeyEvent",
                {
                    "type": "keyDown",
                    "text": ch,
                    "unmodifiedText": ch,
                    "key": ch,
                    **base,
                },
                page_id,
            )
            self.call(
                "Input.dispatchKeyEvent", {"type": "keyUp", "key": ch, **base}, page_id
            )

    def focused_is_editable(self, page_id: str | None = None) -> bool:
        return bool(
            self.ev(
                "(() => { const e = document.activeElement;"
                "return !!e && (e.tagName === 'INPUT' || e.tagName === 'TEXTAREA'"
                " || e.isContentEditable); })()",
                page_id,
            )
        )

    def page_focused(self, page_id: str | None = None) -> bool:
        """Whether the page owns keyboard focus.

        False when the browser is in the background: CDP then accepts the key
        events and drops them, so typing looks like it worked and nothing
        changes. Saying so is the difference between "no effect" and a next
        step.
        """
        try:
            return bool(self.ev("document.hasFocus()", page_id))
        except Exception:
            return False

    def any_page_focused(self) -> bool:
        for page in self.pages():
            try:
                if self.page_focused(page["key"]):
                    return True
            except Exception:
                continue
        return False

    def fill_focused(self, text: str, page_id: str | None = None, clear: bool = True) -> dict:
        """Write into the focused field, or the first visible one, without
        depending on real focus. Returns {'ok': bool, 'value': str|None}.

        Keystrokes go first: they are the only thing an auto-advancing OTP
        form or a framework-controlled input keeps. Assigning `.value` is the
        fallback for the plain inputs that ignore the key pipeline.
        """
        focus = (
            "(() => {"
            "const editable = (e) => !!e && (e.tagName === 'INPUT'"
            "  || e.tagName === 'TEXTAREA' || e.isContentEditable);"
            "let el = document.activeElement;"
            "if (!editable(el)) {"
            "  el = Array.from(document.querySelectorAll("
            "    'input:not([type=hidden]):not([type=submit]),textarea,"
            "     [contenteditable=true]'))"
            "    .find((e) => e.offsetParent !== null && !e.disabled && !e.readOnly);"
            "}"
            "if (!el) return false;"
            "el.focus();"
            "return true;"
            "})()"
        )
        if not self.ev(focus, page_id):
            return {"ok": False, "value": None}
        if clear:
            self.clear_focused(page_id)
        self._send_chars(text, page_id)
        if self.holds(text, page_id):
            return {"ok": True, "value": "".join(self.field_values(page_id))}
        return {"ok": True, "value": self.assign(text, page_id)}

    def alive(self) -> bool:
        return bool(self.pages())
