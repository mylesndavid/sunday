# Sunday desktop (Electron)

The face Sunday wears on macOS. Connects to the local daemon over HTTP +
WebSocket. The one chat lives in the daemon — this is a renderer of it.

## Building for distribution

See [`build/README.md`](build/README.md) for the three paths (unsigned,
signed, signed-and-notarized). Quick start:

```bash
npm install
npm run dist:mac:unsigned    # local DMG for testing
```

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
- `renderer/wake.js` — "Hey Sunday" wake loop (runs in a hidden window).
- `overlay/`     — always-on-top ambient pill.

## Voice + wake word

"Hey Sunday" runs fully on-device. A hidden Sunday window (`wake.html` +
`wake.js`) records short ~2.5s mic windows; `main.js` transcribes each with
the local Whisper pipeline and scans for the wake phrase. On a hit it POSTs
the command to the daemon's `/v1/wake`, which pops the notch (`wake_listening`),
runs the turn, then pushes the answer back (`wake_reply`). Mic capture lives
in-app so the macOS grant is attributed to Sunday — same reason the ambient
observer captures in a Sunday window rather than a detached child.

One-breath commands ("Hey Sunday, what's on my calendar") run immediately; a
bare "Hey Sunday" arms the next window to carry the command. On by default once
onboarded; opt out via `prefs.wake` (`wakeStatus` / `wakeSet` IPC).

Future drop-in upgrades:
- A VAD gate so Whisper only runs on speech (cuts idle CPU/battery).
- Porcupine for instant sub-300ms detection (license + custom keyword model).

## Live view

The daemon broadcasts `browser_frame` / `device_browser_frame` /
`device_screen` events over WS when Sunday is driving a browser or
screen-capturing a device. The renderer surfaces these as inline frames
in the chat. A dedicated "what Sunday's looking at right now" pane lands
in slice G+.
