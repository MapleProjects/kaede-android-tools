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
{"mcp":{"android":{"type":"local","command":["kaede-android-tools"]}}}
```

No device is configured there on purpose. A serial in the client config pins the
phone to one transport: unplug the cable and the same phone answering as
`192.168.1.12:39805` becomes "device not found" until someone edits the JSON and
restarts the server. The tool resolves the target on every call instead - the
device you chose, else a single attached phone - and stops with a list when
several are attached rather than guessing.

## Choosing a phone

Targets live in `~/.config/kaede-android-tools/devices.json` (override the path
with `KAEDE_DEVICE_FILE`). Every string entry is an alias; the three keys below
configure the tool itself.

```json
{
  "main": "192.168.1.12",
  "tablet": "R58R8585EHR",
  "browsers": ["mullvad"],
  "submit_selectors": ["#idSIButton9", "input[type=submit]", "button[type=submit]"],
  "protect": ["~/.config/kaede-android-tools", "~/.config/mimocode"]
}
```

| key | is | why it is here and not in the code |
| --- | --- | --- |
| any other name | device alias, address or serial included | one file answers "which phone", so switching is a data change |
| `browsers` | package substrings that mean "this app has a CDP endpoint" | a browser one person uses is not a rule for everyone |
| `submit_selectors` | CSS tried in order before falling back to Enter | a page that names its own button is a page Enter does not always submit |
| `protect` | folders an assistant must not rewrite while you drive it | a guard needs to know what it is guarding |

```sh
act(action="devices")             # every attached phone, its model, which is in use
act(action="use", text="main")    # switch to an alias; connects it over Wi-Fi if needed
act(action="use", text="192.168.1.12:39805")
```

An alias holding only a host (`192.168.1.12`, no port) is resolved over mDNS,
because wireless debugging hands out a new port every time it is re-enabled. The
switch applies to the running server immediately - no restart, and the cached
`uiautomator2` connection is dropped so the next call lands on the new phone.

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

An icon button frequently has no text, no `content-desc` and no resource id:
Facebook and Google login buttons are plain `ImageView`s, and so are menu and
notification icons. The tree cannot name them, so the row carries its centre
instead, which is also a valid `target`:

```
nameless tappables: #11, #12 — the tree gives them no text, desc or id; each row
carries its @x,y, and see image=true names an icon
#9 view 'Login' tap
#10 btn 'Crear Cuenta' tap
#11 img @440,1582 tap
#12 img @639,1582 tap
```

Take an image when it matters which icon is which. The @x,y tells you the order
without one: left before right, top before bottom.

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
| `tap` | `target` | presses the control a name points at, or long presses with `long=true` |
| `type` | `text`, optional `target` | focuses, clears, writes, optionally `submit=true` |
| `swipe` | `direction` | `up` / `down` / `left` / `right` |
| `key` | `key` | `back`, `home`, `enter`, `delete`, any uiautomator2 key name |
| `launch` | `text` | brings a package up, resuming its task when one exists |
| `foreground` | `text` | resumes a task from recents by its card title |
| `open_url` | `text` | opens a URL in the default browser |
| `wait` | `target` | blocks until the element appears |
| `refresh` | - | reloads the page, or pulls to refresh in native list apps |
| `otp` | optional `text` | every code-shaped number on offer, newest first |
| `devices` | - | every attached phone, its model, and which one is in use |
| `use` | `text` | switches to an alias or serial, connecting it when needed |
| `diag` | - | app, display, keyguard, overlay, crash dialog, newest fatal |
| `crash` | - | the newest fatal exception only |
| `logs` | `text` | logcat filtered by a pattern |
| `shell` | `text` | raw `adb shell`, for package management and diagnostics |
| `info` | `target` | every node a name or a point reaches, and whether it can be pressed |

`swipe` also takes explicit geometry, `target="540,300" text="540,1700"`, for
gestures like pull-to-refresh that must start near the top edge.

### Text and buttons are not the same thing

One screen often carries the same words twice: a heading "Iniciar sesión" over
the button that logs in, a card title over the pane that opens it, a settings
label whose row is missing from the tree. Pressing the heading does nothing, and
"nothing" looks exactly like a tap that worked, so the agent walks into the next
screen believing it pressed something.

Three rules keep that out of the picture:

- `tap` presses the control the name belongs to. A label that sits inside a
  clickable row, or inside a pane that takes the touch, is pressed through it,
  at the spot where the words actually are — a card's wrapper is the whole
  window, and its own middle can be the status bar.
- A node that carries a name but answers to no finger is shown as
  `no-handler`. A toolbar title inside an inert wrapper is a label, not a button.
- When the screen is identical after the tap, the result says so instead of
  saying `ok`.

`info` answers the question before the tap instead of after it:

```
info text=Iniciar sesión
2 node(s) match 'text=Iniciar sesión'
  txt      'Iniciar sesión' plain text — a tap here only does something if a control wraps it
           bounds=[200, 480, 880, 560] centre=(540, 520) listens=nothing
  btn      'Iniciar sesión' pressable — the touch lands on it
           bounds=[126, 1250, 954, 1382] centre=(540, 1316)
tap 'text=Iniciar sesión' would press btn 'Iniciar sesión' at @540,1316
```

It presses nothing. A point works too, `info 540,1430`, and lists every node
under it from the smallest up, which is how a name shared by a title and a
button gets told apart.

`foreground` exists because `launch` restarts an app: a half-finished OAuth
custom tab would start over. It opens recents and taps the card whose title
matches, so the same `target` forms work (`text=Teclado Microsoft SwiftKey`).

`otp` collects the candidates from two places at once — the posted
notifications (read straight from the notification service, no shade to open)
and the text of the current screen in reading order — and prints the line each
code came from:

```
otp: 2 candidate(s), newest first
444919  notification com.google.android.gm 12:29  Tu código de un solo uso es: 444919
on screen, top to bottom (y is the row's top edge); in a mail or chat list the
newest is the last one, and that is the only code the login will accept:
  300  12:21
 1500  Tu código de un solo uso es: 765780
 1560  12:29
 1700  Tu código de un solo uso es: 444919   <- newest on screen
```

That annotation is not decoration. A thread view recycles its rows, the text
dump comes back in document order, and the code that reads as "the last one" is
regularly the older of the two. Typing the older code is exactly how a login
answers "ese código es incorrecto".

`type` switches apps by itself when the field it is aiming at belongs to an app
that is not in front. A keystroke cannot be aimed at a window that is not in
front, and asking the caller to notice that and issue a second call is a round
trip it should not have to make:

```
act(action="type", text="444919")     # Gmail is on screen, the login tab is behind
→ switched to com.android.chrome (resumed its task)
```

The switch is `am start --activity-reorder-to-front` on the existing task's own
root activity, which brings the task forward instead of starting anything, so a
half-filled form survives. A task whose activity is not exported — Chrome's
custom tabs, the very surface a Microsoft login uses — is refused by the system,
and those fall back to tapping their recents card, verified against the package
because cards carry the app label and not the package name. Only when both fail
does `type` refuse and say why, so nothing is ever written into whatever happened
to be on screen.

Naming a field (`target=#13`) turns that off on purpose. Aiming at a field is an
instruction to type there, and the app in front is then the destination by
definition: a login hosted in a WebView (microG, an in-app browser) owns its
field but has no CDP endpoint, so the browser page open in the background is not
its owner and switching to it would discard the very field that was pointed at.


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
| `type` | types character by character into the field, on whichever tab has one |
| `reload` | reloads the page |
| `open` | loads a URL in the phone's browser |

Without `page=`, `type` tries the tabs in the order a person would: the one
that owns the keyboard first, then the rest, and it stops on the first tab whose
field actually holds the text. Two tabs in different renderer processes can both
report focus, and the one reporting it is not always the one with a form.

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
act(action="otp", text="microsoft")       # candidates, newest first, with the line
act(action="type", text="444919")         # switches back to the login tab itself
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
