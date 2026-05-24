# Sunday

Sunday is a personal AI. Built for one person at a time. Knows you, remembers everything, gets better at knowing you with every conversation.

Not an assistant. Not a chatbot. Closer to a friend who happens to have a perfect memory and is always paying attention.

## Status

v0.0.1 — scaffolded 2026-05-24. The repo is a fresh start. Cherry-picks coming in from three sources:

- the prior Sunday prototype (lightweight Python daemon + Electron shell + voice stack) — preserved at `~/Repos/sunday-old/`
- BetterBot (graph memory pipeline, iMessage channel, voice-WS server) — preserved at `~/Repos/betterbot/`
- Hermes / dcharness (subprocess-style agent loop, "just works" minimal runtime)

## Identity

The product is one thing: a personal AI that knows what's going on in *your* life, for you, and gets better at it over time. Voice-first. Single-agent. No team, no platform, no work-tool sprawl.

The identity prompt lives in `src/sunday/prompt.py` and is load-bearing — every turn the model sees it.

## Layout

```
src/sunday/
  __init__.py
  prompt.py     — Sunday's identity (the system prompt)
  config.py     — model + memory + voice config
                  (default model: DeepSeek V4 Flash)
pyproject.toml  — package metadata, deps
```

Daemon, CLI, memory, tools all coming next.

## Running

Not runnable yet. Scaffold only.

## License

Proprietary. All rights reserved.
