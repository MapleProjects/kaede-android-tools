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
            # Prefer a real document over chrome-native:// placeholder tabs.
            web_pages = [p for p in pages if p["url"].startswith("http")]
            return (web_pages or pages)[0]
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
    def fill(self, selector: str, text: str, page_id: str | None = None) -> str:
        script = (
            "(() => {"
            f"const el = document.querySelector({json.dumps(selector)});"
            "if (!el) return {ok:false, error:'selector matched nothing'};"
            "el.focus();"
            "const proto = el.tagName === 'TEXTAREA'"
            "  ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype;"
            "const setter = Object.getOwnPropertyDescriptor(proto, 'value').set;"
            f"setter.call(el, {json.dumps(text)});"
            "el.dispatchEvent(new Event('input', {bubbles:true}));"
            "el.dispatchEvent(new Event('change', {bubbles:true}));"
            "return {ok:true, value: el.value};"
            "})()"
        )
        out = self.ev(script, page_id) or {}
        if not out.get("ok"):
            raise RuntimeError(out.get("error", "fill failed"))
        return str(out.get("value", ""))

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

    def type_text(self, text: str, page_id: str | None = None) -> bool:
        """Focus the first usable field and type it like a person would.

        Real key events matter: one-shot value assignment leaves OTP boxes
        empty because their auto-advance handlers never fire.
        """
        self.ev(
            "(() => { const ok = (e) => !!e && (e.tagName === 'INPUT'"
            "  || e.tagName === 'TEXTAREA' || e.isContentEditable);"
            "let el = document.activeElement;"
            "if (!ok(el)) { el = [...document.querySelectorAll("
            "  'input:not([type=hidden]),textarea,[contenteditable=true]')]"
            "  .find(e => e.offsetParent !== null && !e.disabled && !e.readOnly); }"
            "if (el) { el.focus(); return 1; } return 0; })()",
            page_id,
        )
        for ch in text:
            base = {"windowsVirtualKeyCode": ord(ch.upper()), "nativeVirtualKeyCode": ord(ch)}
            self.call(
                "Input.dispatchKeyEvent",
                {"type": "keyDown", "text": ch, "key": ch, "unmodifiedText": ch, **base},
                page_id,
            )
            self.call(
                "Input.dispatchKeyEvent",
                {"type": "keyUp", "key": ch, **base},
                page_id,
            )
        value = self.ev(
            "(() => { const e = document.activeElement;"
            " return e ? String(e.value ?? e.textContent ?? '') : ''; })()",
            page_id,
        )
        return bool(value)

    def focused_is_editable(self, page_id: str | None = None) -> bool:
        return bool(
            self.ev(
                "(() => { const e = document.activeElement;"
                "return !!e && (e.tagName === 'INPUT' || e.tagName === 'TEXTAREA'"
                " || e.isContentEditable); })()",
                page_id,
            )
        )

    def fill_focused(self, text: str, page_id: str | None = None) -> dict:
        """Write into the focused field, or the first visible one, without
        depending on real focus. Returns {'ok': bool, 'value': str|None}."""
        script = (
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
            "if (!el) return {ok:false, value:null};"
            "el.focus();"
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
        return self.ev(script, page_id) or {"ok": False, "value": None}

    def alive(self) -> bool:
        return bool(self.pages())
