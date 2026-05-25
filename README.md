# Sunday

A personal AI you self-host. Knows you, remembers everything, gets better at knowing you over time. Voice, text, iMessage, phone calls — all flowing into **one chat** that's yours.

Not a chatbot. Not a SaaS. A daemon you run, with satellites you install on each computer you own, and a desktop UI that talks to it.

```
your Mac / phone / desktop ──┐
your second Mac (satellite) ─┼──→ Sunday daemon ──→ your LLM provider
your VPS (optional)         ─┘     (one chat log)
```

## The opinion

There is exactly one conversation between you and Sunday — ever. iMessage, voice, the desktop app, the CLI, an outbound phone call she places for you, a browser session she's driving — those are *modalities*, not threads. Every message lands in the same SQLite log. The model sees the same context regardless of which surface you used.

## What's in the box

| Subsystem | What it does |
| --- | --- |
| **Daemon** | Asyncio Python server. Unix-socket + HTTP + WebSocket. Owns the one chat. |
| **Brain** | OpenAI-compatible streaming with tool calls. Default runtime: OpenRouter (any model). Hermes CLI also supported. |
| **Channels** | Inbound paths: Sendblue webhook (text Sunday from anywhere), HTTP, WS. Outbound: Sendblue API, iMessage via satellite, VAPI calls. |
| **Devices** | Install `sunday-satellite` on any Mac. It dials home over WebSocket and exposes shell, screen capture, CDP browser control, and (on macOS) iMessage read+send. |
| **Cloud tools** | Cloudflare Browser Rendering for headless browsing + screenshots. Optional Cloudflare Sandbox Worker for untrusted code. |
| **Electron app** | Single-column chat UI, warm-dark theme, streaming token-by-token. Drag-drop files, voice via `SpeechRecognition`, "Hey Sunday" wake word. |

## Quick start (self-hosted, local)

```bash
git clone https://github.com/<you>/sunday.git
cd sunday

# Install
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[devices]"

# Credentials
cp .env.example ~/.sunday/credentials.env
$EDITOR ~/.sunday/credentials.env        # at minimum, set OPENROUTER_API_KEY

# Start the daemon (foreground)
sunday start
```

In a second terminal:

```bash
sunday status
sunday say "hey — first time we're talking"
sunday log -n 10
sunday tools
```

### Desktop app

```bash
cd electron
npm install
npm start
```

Or point Electron at a remote daemon:

```bash
SUNDAY_DAEMON_HTTP=https://your-sunday.example.com \
SUNDAY_DAEMON_WS=wss://your-sunday.example.com/v1/ws \
npm start
```

## Connecting a satellite

Each computer you want Sunday to *see* runs a satellite — a tiny long-running process that dials home to your daemon and serves tool calls (screenshots, shell commands, CDP browser, iMessage). On a Mac:

```bash
# On the satellite machine
git clone https://github.com/<you>/sunday.git
cd sunday
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[devices]"

sunday-satellite \
  --server ws://your-sunday.example.com:8765/v1/devices/ws \
  --device-id $(hostname -s)
```

The satellite auto-advertises capabilities: `shell`, `screen`, `cdp`. On macOS it also adds `imessage` when `~/Library/Messages/chat.db` is readable — that requires giving the process **Full Disk Access** (System Settings → Privacy & Security → Full Disk Access).

Once connected, `sunday tools` on the central daemon shows `device_run_command`, `device_screenshot`, `device_cdp_*`, and (if a Mac satellite is connected) `imessage_*`. The brain routes them automatically.

## Self-hosting on a VPS

Sunday runs anywhere Python 3.10+ runs. Reference setup:

1. **Provision a small VPS** (1–2 GB RAM is plenty).
2. **Install Caddy** as the reverse proxy — handles TLS via Let's Encrypt automatically.
3. **Rsync the repo** + create a `systemd` unit:

```ini
# /etc/systemd/system/sunday.service
[Unit]
Description=Sunday personal AI daemon
After=network.target

[Service]
Type=simple
User=root
WorkingDirectory=/opt/sunday
Environment=SUNDAY_HOME=/root/.sunday
ExecStart=/opt/sunday/.venv/bin/sunday start
Restart=always

[Install]
WantedBy=multi-user.target
```

4. **Caddyfile block**:

```
your-sunday.example.com {
    @websocket {
        header Connection *Upgrade*
        header Upgrade websocket
    }
    handle @websocket {
        reverse_proxy localhost:8765 {
            transport http { read_timeout 0; write_timeout 0 }
        }
    }
    handle {
        reverse_proxy localhost:8765 { flush_interval -1 }
    }
}
```

5. **Point DNS** at the VPS (A record, "DNS only" if behind Cloudflare so Caddy can handle HTTP-01 challenge), then `systemctl reload caddy`. The cert issues in seconds.

Now `https://your-sunday.example.com/v1/status` returns from your daemon.

## Optional integrations

Each is opt-in — Sunday just won't register tools she lacks credentials for.

- **Sendblue** (https://sendblue.co): text Sunday at a real iMessage-capable phone number. Point Sendblue's inbound webhook at `https://your-sunday.example.com/webhooks/sendblue`.
- **VAPI** (https://vapi.ai): Sunday makes outbound phone calls via the `call_phone` tool. Transcripts get written back into the chat when the call ends.
- **Cloudflare Browser Rendering** (https://developers.cloudflare.com/browser-rendering/): `browser_navigate`, `browser_screenshot`, `browser_scrape` tools. Token needs *Browser Rendering: Edit* permission.
- **Cloudflare Sandbox Worker**: untrusted code execution via the `sandbox_run` tool. Deploy your own Worker and set `SANDBOX_WORKER_URL`.

## Layout

```
src/sunday/
  prompt.py            Sunday's identity (the system prompt)
  config.py            Model + memory + voice + server configs
  paths.py             ~/.sunday/ layout
  credentials.py       Local credential store, 0600 perms
  attachments.py       Files / images / multi-modal attachment helpers
  chat.py              The one chat (SQLite at ~/.sunday/sunday.db)
  brain.py             Tool-call loop with streaming
  tools.py             ToolRegistry + ToolContext
  ipc.py               JSON-RPC over Unix socket
  daemon.py            asyncio Unix-socket + HTTP + WebSocket + webhooks
  cli.py               `sunday` entry point
  runtime.py           Runtime protocol + build_runtime
  runtime_openai.py    OpenAI-compatible streaming (default)
  runtime_hermes.py    Subprocess Hermes CLI (alternate)
  subagents/hermes.py  delegate_to_hermes — scoped sub-task tool
  channels/
    messages_local.py  iMessage proxy tools (dispatch through satellite)
    sendblue.py        Sendblue webhook + outbound + typing indicator
    vapi.py            VAPI outbound call tool + call-end webhook
  cloud/
    cloudflare.py      Browser Rendering + Sandbox tools
  devices/
    protocol.py        Device wire frames
    manager.py         Main-side DeviceManager
    cdp.py             Chromium shadow-profile launcher + CDP driver
    imessage_macos.py  chat.db read + osascript send (runs on satellite)
    satellite.py       `sunday-satellite` entry point
    tools.py           Brain-side device tools (run_command, screenshot, cdp_*)
electron/
  main.js              Electron main process (window + overlay + IPC)
  preload.js           Context bridge
  renderer/            Chat UI (vanilla JS, no framework)
    index.html, styles.css, app.js, wake.js
  overlay/             Always-on-top ambient pill
```

## Architecture notes

- **One chat** is the load-bearing opinion. SQLite, append-only, every message tagged with its modality. No threads. No conversations-per-app. One.
- **Brain on the daemon, tools wherever they make sense.** Device tools execute on satellites. Channel tools execute on the daemon. Cloud tools execute via Cloudflare. The brain doesn't know or care where; the tool registry handles it.
- **Streaming end-to-end.** OpenAI streaming → daemon emits `stream_delta` events over WS → Electron renders token-by-token with a pulsing caret.
- **Webhooks are webhooks.** No polling. If your inbound channel drops a message, that's a bug in the channel — not something to paper over with a 30s poll.
- **No vendor lock.** Brain swaps with one env var. Channels are plugins. Anything can be replaced.

## Contributing

This is a young project. Issues + PRs welcome. The codebase is intentionally small and readable — `daemon.py` + `brain.py` are the entire runtime in ~500 lines.

## License

MIT — see [LICENSE](LICENSE).
