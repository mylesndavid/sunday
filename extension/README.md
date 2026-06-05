# Sunday Cockpit (browser extension)

Connects Chrome to Sunday's daemon so Sunday can work in the browser with you:
read pages, click, fill forms, manage tabs, take screenshots — and hand control
back to you for anything sensitive (passwords, one-time codes, payments,
consent screens are never touched by the agent; the `fill` executor refuses
credential fields outright).

Derived from the Cockpit extension with its built-in agent loop removed:
Sunday's daemon is the brain, this extension is the hands.

## Install (developer mode)

1. Open `chrome://extensions`
2. Turn on "Developer mode" (top right)
3. Click "Load unpacked" and pick this `extension/` folder
4. Click the Sunday Cockpit toolbar icon and copy the pairing token
5. Paste the token into Sunday when it asks to connect your browser

## How pairing works

- The extension generates a token on install and shows it in its popup.
- It connects out to `ws://127.0.0.1:8765/v1/cockpit/ws?token=<token>` and
  retries with backoff until Sunday is running. Localhost only — nothing
  leaves the machine.
- The port is configurable on the options page (default 8765).

## Protocol

JSON text frames over the WebSocket:

| direction | frame |
|---|---|
| daemon → extension | `{id, method, params}` |
| extension → daemon | `{id, result}` or `{id, error}` |
| extension → daemon (unsolicited) | `{event: "status", ...}` / `{event: "user_done", ...}` / `{event: "say", text}` |
| daemon → extension (chat push) | `{event: "thinking"}` then `{event: "reply", text}` |

Methods: `read_page`, `screenshot`, `navigate`, `click`, `fill`, `press_key`,
`scroll`, `highlight`, `list_tabs`, `open_tab`, `switch_tab`, `close_tab`,
`instruct_user`.

`instruct_user` shows an on-page callout (optionally highlighting elements),
pauses, and resolves with `{acknowledged: true}` when the user clicks
"Done — continue". A `{event: "user_done"}` frame is also emitted.

`read_page` returns `{url, title, elementCount, elements, pageText, scroll}`
where `elements` is a numbered list (`[7] <button> "Sign in"`); those indexes
feed `click`/`fill`/`press_key`/`scroll`/`highlight`/`instruct_user` and are
valid until the page changes.

## Layout

- `background.js` — service worker: WebSocket bridge, token, reconnect, dispatch
- `lib/executor.js` — browser tool executors (tabs, scripting, screenshots)
- `content.js` — in-page perception (element indexing) + actions + callouts
- `popup.html/js` — pairing token + connection status (+ "Chat" opens the side panel)
- `sidepanel.html/js` — side-panel chat with Sunday over the paired socket
- `options.html/js` — daemon port
