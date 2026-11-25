# kaede-android-tools

A minimal MCP server that lets an agent drive a real Android phone the way a
person does: read the screen, tap what it sees, type into fields, and reach into
web pages when an app embeds a browser.

Three tools. No coordinate guessing. Every action returns the new screen, so a
single call is usually enough.

## Why

Most Android MCP servers expose a pile of thin adb wrappers, which forces the
agent to take a screenshot, eyeball pixel positions and tap blind. That breaks
the moment a layout shifts, and it burns tokens on every round trip.

This server fixes the three failure modes that actually hurt:

| Problem | What this does instead |
| --- | --- |
| Tapping guessed pixel coordinates | Resolves elements from the accessibility tree and taps their centre |
| Blind actions, then a second call to see what happened | Every action returns the resulting compact screen |
| Filling a form inside a WebView | Falls back to Chrome DevTools `Input.insertText` when native input does nothing |

It talks to three layers under the hood and picks the right one per situation:

- **adb** for shell, app launch, URLs and screenshots.
- **uiautomator2** for the accessibility tree and element-level actions. Its
  agent is pushed automatically, no root required.
- **Chrome DevTools Protocol** for web content. Debug sockets are discovered and
  forwarded automatically, including the per-process sockets that custom tabs
  use.

## Install

```sh
uv tool install git+https://github.com/MapleProjects/kaede-android-tools
```

or from a clone:

```sh
uv venv && uv pip install -e .
```

Requires `adb` in `PATH`, a phone with USB debugging authorised, and Python 3.10+.

## Wire it into an MCP client

```json
{
  "mcp": {
    "android": {
      "type": "local",
      "command": ["kaede-android-tools"],
      "environment": { "ANDROID_SERIAL": "R58R8585EHR" }
    }
  }
}
```

`ANDROID_SERIAL` is optional. Without it the only connected device is used.

## The three tools

### `see(image=false, full=false, text=false)`

Returns the current screen as a compact, numbered list.

```
app=com.android.chrome act=ChromeTabbedActivity 1080x2400 kb=off browser=1
#1 btn 'Cerrar' tap
#2 txt 'Iniciar sesión'
#3 in 'Correo electrónico o número de teléfono'
#4 btn 'Siguiente' tap
```

Those `#N` references are stable for the next action. They are re-anchored on
the live tree by text, resource id and class, so a relayout does not break them.
Inputs are anchored by resource id and position instead, because typing rewrites
their own text.

`text=true` drops the references and returns only the readable text, which is
what you want for pulling a verification code out of an email:

```
see(text=true)
→ Recibidos
  No leída, Equipo de cuentas Microsoft, 3 mensajes, Tu código de un solo uso,
  ... Tu código de un solo uso es: 873540 ...
```

If a dialog steals focus from the app (a permission prompt, the "open with"
sheet, a "continue to the app" box) the header says so, because that overlay is
invisible in the app's own tree and is usually the thing blocking progress:

```
app=com.touchtype.swiftkey act=... OVERLAY=com.android.intentresolver/.ChooserActivity
```

### `act(action, target=None, text=None, ...)`

One verb, one call, and the resulting screen comes back with it.

| action | needs | does |
| --- | --- | --- |
| `tap` | `target` | taps the element centre, or long presses with `long=true` |
| `type` | `text`, optional `target` | focuses, clears, writes, optionally `submit=true` |
| `swipe` | `direction` | `up` / `down` / `left` / `right` |
| `key` | `key` | `back`, `home`, `enter`, `delete`, any uiautomator2 key name |
| `launch` | `text` | starts a package and waits for it |
| `open_url` | `text` | opens a URL in the default browser |
| `wait` | `target` | blocks until the element appears |
| `refresh` | - | reloads the page, or pulls to refresh in native list apps |
| `shell` | `text` | raw `adb shell`, for package management and diagnostics |

`swipe` also takes explicit geometry, `target="540,300" text="540,1700"`, for
gestures like pull-to-refresh that must start near the top edge.

`target` accepts four forms:

```
#3                       a reference from see()
text=Siguiente           exact label
~siguiente               label contains, case insensitive
id=com.app:id/button     resource id contains
540,1200                 explicit coordinates, last resort
```

### `web(action, selector=None, script=None, text=None, url=None, page=None)`

For whatever lives inside a browser surface.

| action | does |
| --- | --- |
| `pages` | lists tabs across every Chrome process with their page ids |
| `text` | lists the visible text of the page |
| `eval` | runs JavaScript and returns the JSON result |
| `fill` | sets an input's value through the native setter, so React notices |
| `click` | clicks a CSS selector |
| `click_text` | clicks the element whose visible text matches, no CSS needed |
| `press` | trusted key event: `enter`, `tab`, `escape`, `backspace` |
| `type` | types character by character into the focused field |
| `reload` | reloads the page |
| `open` | loads a URL in the phone's browser |

Two of these exist because the obvious version does not work. `el.click()` and
synthetic `KeyboardEvent`s are untrusted, so auth pages ignore them; `click` and
`click_text` dispatch real mouse events and `press` dispatches real key events.
And `type` sends one key event per character, because assigning the value of a
six-box OTP field leaves all six boxes empty. Its auto-advance runs on keystrokes,
not on values.

## Examples

Log into a native app:

```
act(action="launch", text="com.touchtype.swiftkey")
see()
act(action="tap", target="text=Cuenta")
act(action="type", target="~Correo", text="me@example.com", submit=true)
```

Fill a web form and read the result:

```
web(action="pages")
web(action="fill", selector="input[type=email]", text="me@example.com")
web(action="click", selector="button[type=submit]")
web(action="eval", script="location.href")
```

Read a one-time code out of the inbox and use it, without leaving the app:

```
act(action="launch", text="com.google.android.gm")
act(action="refresh")                     # pull to refresh, new mail shows up
see(text=true)                            # the code is in the list snippet
web(action="type", page="9401:1", text="873540")   # real keystrokes, auto-submits
```

Assert that a flow reached the right place:

```
act(action="wait", target="text=Bienvenido", timeout=15)
```

## Notes

- Element references are per snapshot. After a screen change, take a fresh
  `see()` or rely on the screen that `act` returns.
- `type` verifies what it wrote. If the field is still empty and the foreground
  app is a browser, it retries through CDP and tells you which path it used.
- Nothing here needs root, and nothing modifies the system. The uiautomator2
  agent lives in `/data/local/tmp` and leaves no installed package behind.
- `adb forward` ports are chosen from 9400 upwards to stay clear of 9222, which
  desktop Chrome tooling usually occupies.

## License

MIT
