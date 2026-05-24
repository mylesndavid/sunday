# Sunday desktop (Electron)

The face Sunday wears on macOS. Connects to the local daemon over HTTP +
WebSocket. The one chat lives in the daemon — this is a renderer of it.

## Dev

```bash
# Start the daemon first (separate terminal)
cd ..
pip install -e .
export DEEPSEEK_API_KEY=sk-...
sunday start

# Then run the Electron app
cd electron
npm install
npm start
```

The window connects to `http://127.0.0.1:8765` by default. Override with:

```bash
SUNDAY_DAEMON_HTTP=http://localhost:8765 \
SUNDAY_DAEMON_WS=ws://localhost:8765/v1/ws \
npm start
```

## Layout

- `main.js`      — Electron main process. Window + overlay + IPC.
- `preload.js`   — context bridge.
- `renderer/`    — main chat window. `app.js` is the whole UI logic.
- `renderer/wake.js` — "Hey Sunday" continuous SpeechRecognition listener.
- `overlay/`     — always-on-top ambient pill.

## Voice + wake word

v0.1 uses the browser's `SpeechRecognition` (free, ships with Chromium).
"Hey Sunday" triggers a focused single-utterance recording that lands in
the composer and sends to the daemon.

Future drop-in upgrades:
- Porcupine Web for offline wake word (license required).
- Whisper via a `/v1/voice/transcribe` daemon route for higher accuracy.

## Live view

The daemon broadcasts `browser_frame` / `device_browser_frame` /
`device_screen` events over WS when Sunday is driving a browser or
screen-capturing a device. The renderer surfaces these as inline frames
in the chat. A dedicated "what Sunday's looking at right now" pane lands
in slice G+.
