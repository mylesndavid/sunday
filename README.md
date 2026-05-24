# Sunday

Sunday is a personal AI. Built for one person at a time. Knows you, remembers everything, gets better at knowing you with every conversation.

Not an assistant. Not a chatbot. Closer to a friend who happens to have a perfect memory and is always paying attention.

## The opinion

There is exactly one chat between you and Sunday, ever. iMessage, voice, the desktop app, the CLI, an outbound phone call she places for you, a browser session she's driving on your behalf — those are modalities, not threads. Every message lands in the same log. Sunday sees the same context no matter which interface you used.

## What's wired

| Slice | Subsystem | Status |
| --- | --- | --- |
| 1, A | One chat (SQLite) + daemon + CLI + HTTP/WS server + tool-call loop | ✅ |
| B    | Hermes as primary runtime, DeepSeek-compatible OpenAI as fallback | ✅ |
| C    | iMessage: read user's chat.db, send via Messages.app, Sendblue inbound | ✅ |
| C+   | Files + images + multi-text attachments end-to-end (chat → LLM → channels) | ✅ |
| D    | VAPI outbound voice calls + transcript drop into chat | ✅ |
| E    | Cloudflare Browser Rendering + Sandbox + live-frame WS broadcast | ✅ |
| F    | Remote-device satellite + CDP shadow profile + screen + shell tools | ✅ |
| G    | Electron desktop app: chat UI + voice + Hey Sunday wake word + drag-drop | ✅ |

## Repo layout

```
src/sunday/
  prompt.py            Sunday's identity (the system prompt)
  config.py            Model + memory + voice + server + Hermes + Cloudflare + VAPI config
  paths.py             ~/.sunday/ layout
  credentials.py       ~/.sunday/credentials.env (mode 0600)
  attachments.py       Files / images / multi-modal attachment helpers
  chat.py              The one chat. SQLite at ~/.sunday/sunday.db.
  brain.py             Tool-call loop. Identity prompt + chat → reply.
  tools.py             ToolRegistry + ToolContext. default_registry composes
                       all subsystems lazily.
  ipc.py               JSON-RPC over Unix socket.
  daemon.py            asyncio: Unix-socket + HTTP + WebSocket + webhooks.
  cli.py               `sunday` entry point.
  runtime.py           Runtime protocol. build_runtime() picks Hermes or OpenAI.
  runtime_openai.py    Direct OpenAI-compatible (DeepSeek by default).
  runtime_hermes.py    Subprocess `hermes chat`. Parses fenced ```tool``` blocks.
  subagents/
    hermes.py          delegate_to_hermes — scoped sub-task in a fresh process.
  channels/
    messages_local.py  Local iMessage read+send (chat.db + AppleScript).
    sendblue.py        Sendblue webhook + send.
    vapi.py            VAPI outbound phone calls + webhook.
  cloud/
    cloudflare.py      Browser Rendering tools + Sandbox runner.
  devices/
    protocol.py        Device wire frames.
    manager.py         Main-side DeviceManager. Dispatches commands.
    cdp.py             Chromium shadow-profile launch + CDP driver.
    satellite.py       `sunday-satellite` entrypoint for remote machines.
    tools.py           Brain-side tools (device_run_command, device_cdp_*, etc.).
electron/
  package.json
  main.js              Electron main process. Window + overlay + IPC.
  preload.js           Context bridge.
  renderer/            The chat UI (vanilla JS, no framework).
    index.html
    styles.css         Warm-dark theme, amber accent, one column.
    app.js             WS to daemon, message rendering, send, voice, drag-drop.
    wake.js            "Hey Sunday" continuous SpeechRecognition listener.
  overlay/             Always-on-top ambient state pill.
```

## Running it

### Daemon

```bash
cd ~/Development/Repos/sunday
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[devices]"

# Credentials (any combo of env vars or stored)
sunday credential set OPENROUTER_API_KEY   sk-or-...  # default route for the model
                                                      # (Hermes runtime also uses OpenRouter)
sunday credential set SENDBLUE_API_KEY_ID  ...      # optional, for Sendblue channel
sunday credential set SENDBLUE_API_SECRET_KEY ...
sunday credential set VAPI_API_KEY         ...      # optional, for outbound calls
sunday credential set VAPI_PHONE_NUMBER_ID ...
sunday credential set CLOUDFLARE_API_TOKEN ...      # optional, for browser tools
sunday credential set CLOUDFLARE_ACCOUNT_ID ...

# Foreground (Ctrl+C to stop)
sunday start
```

Verify in a second terminal:

```bash
sunday status
sunday tools
sunday say "hey, what do you actually know about me right now?"
sunday log -n 10

# Send with an attachment
sunday say "what's in this?" --attach ~/Pictures/Screenshot.png
```

### Electron desktop

```bash
cd electron
npm install
npm start
```

It connects to `http://127.0.0.1:8765` by default. The window renders the same one chat the CLI writes to. Say "Hey Sunday" to start a voice utterance — the wake word listens continuously via the browser's built-in SpeechRecognition.

### Remote devices

On any other Mac you want Sunday to be able to see + drive:

```bash
pip install -e ".[devices]"
sunday-satellite \
  --server ws://YOUR-MAIN-MAC.local:8765/v1/devices/ws \
  --device-id mac-studio
```

It registers, and `sunday tools` on the main daemon will show
`device_run_command`, `device_screenshot`, `device_cdp_launch`, etc.

### iMessage prerequisites

For Sunday to read your iMessages and respond:

1. Sign Messages.app in to your iMessage account.
2. Give the process running the daemon **Full Disk Access**
   (System Settings → Privacy & Security → Full Disk Access → add Terminal,
   your IDE, or the Sunday binary).

Read-only access to `~/Library/Messages/chat.db` covers receiving;
sending uses `osascript` against Messages.app so replies appear in your
own iMessage history exactly as if you typed them.

## What's next

- Slice H: memory — port BetterBot's graph extraction (entity + relationship
  + recall) into `memory.py`, backed by SQLite + sqlite-vec. This is the
  "knows everything about you" core.
- Slice I: proactive — calendar / email watchers that surface upcoming things
  into the chat before you ask.
- Slice J: Whisper voice ingest via a `/v1/voice/transcribe` daemon route,
  and Porcupine for offline wake-word in the Electron app.
- Slice K: human-in-the-loop browser sessions — keep a Cloudflare browser
  session alive, stream frames live, accept user clicks/typing back through
  the WS.

## License

Proprietary. All rights reserved.
