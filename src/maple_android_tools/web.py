"""Chrome DevTools layer: discovers the phone's debug sockets, forwards them and
speaks CDP. No port juggling for the caller."""

from __future__ import annotations

import json
import re
import urllib.request

SOCKET_RE = re.compile(r"@([A-Za-z0-9_.:-]*devtools_remote[A-Za-z0-9_.:-]*)")
BASE_PORT = 9400


class Web:
    def __init__(self, device) -> None:
        self.device = device
        self._forwarded: dict[str, int] = {}
        self._next_port = BASE_PORT

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

    def pages(self) -> list[dict]:
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
        return found

    def _pick(self, page_id: str | None) -> dict | None:
        pages = self.pages()
        if not pages:
            return None
        if page_id is None:
            return pages[0]
        for page in pages:
            if page["key"] == page_id:
                return page
        for page in pages:
            if page["id"] == page_id or page["url"].startswith(page_id):
                return page
        return None

    # ----------------------------------------------------------------- cdp
    def _ws(self, page: dict):
        import websocket

        return websocket.create_connection(page["ws"], timeout=15, suppress_origin=True)

    def call(self, method: str, params: dict | None = None, page_id: str | None = None):
        page = self._pick(page_id)
        if page is None:
            raise RuntimeError("no debuggable page found; open a browser tab first")
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
