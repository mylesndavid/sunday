# Sunday

Sunday is a personal AI. Built for one person at a time. Knows you, remembers everything, gets better at knowing you with every conversation.

Not an assistant. Not a chatbot. Closer to a friend who happens to have a perfect memory and is always paying attention.

## The opinion

There is exactly one chat between you and Sunday, ever. iMessage, voice, the desktop app, the CLI — those are modalities, not threads. Every message lands in the same log. Sunday sees the same context no matter which interface you used.

## Status

v0.1 — daemon + CLI working end-to-end. Voice, Electron, iMessage coming next.

## Layout

```
src/sunday/
  prompt.py        Sunday's identity (the system prompt)
  config.py        Model + memory + voice config. Default model: DeepSeek V4 Flash.
  paths.py         ~/.sunday/ layout
  credentials.py   ~/.sunday/credentials.env (mode 0600)
  chat.py          The one chat. SQLite at ~/.sunday/sunday.db.
  brain.py         One LLM call per turn.
  ipc.py           JSON-RPC over Unix socket.
  daemon.py        Background process. Owns the chat + brain.
  cli.py           `sunday` entry point.
```

## Running it

```bash
# Install in editable mode
pip install -e .

# Set your DeepSeek key (env var or stored)
export DEEPSEEK_API_KEY=sk-...
# or: sunday credential set DEEPSEEK_API_KEY sk-...

# Terminal 1 — daemon in the foreground
sunday start

# Terminal 2 — talk to her
sunday status
sunday say "hey, how are you?"
sunday log -n 10
sunday stop
```

The chat persists across daemon restarts — same log, same Sunday.

## What's coming

- **Slice 2**: Electron desktop app, cherry-picked from the prior Sunday prototype (`~/Repos/sunday-old/`). Same one-chat log, nicer face.
- **Slice 3**: Voice modality. OpenAI Realtime via the Electron renderer.
- **Slice 4**: Memory. Ported from `~/Repos/betterbot/`'s graph memory pipeline into `memory.py` — the "knows everything about you" core.
- **Slice 5**: Channels. iMessage (Sendblue), then phone calls (VAPI). All writing to the same chat.

## License

Proprietary. All rights reserved.
