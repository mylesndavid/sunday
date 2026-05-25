# sunday/runtime — credits

Sunday's runtime borrows patterns and a handful of utility functions from
**Hermes** by Nous Research (https://github.com/NousResearch/hermes,
MIT-licensed, copyright (c) 2025 Nous Research). Hermes is one of the
better-hardened open-source agent runtimes in the wild and we'd be
foolish to rebuild what they've already field-tested.

Files that contain verbatim or lightly adapted code from Hermes carry a
`# Adapted from hermes/run_agent.py — MIT — (c) 2025 Nous Research`
header at the top, plus a more specific reference next to the function.

Specifically:
- `iteration_budget.py` — `IterationBudget` class, ported verbatim.
- `tool_args.py` — `_repair_tool_call_arguments` + helpers, ported verbatim.

The rest of this package (the agent loop in `core.py`, the provider
adapters in `providers/`, the streaming wiring) is Sunday's own code,
written in the Hermes shape but not copied from it. Hermes itself is
roughly 15× larger than Sunday and includes a lot we don't need (TUI,
gateway platforms, multi-tenant auth, etc.); we only fork what's
load-bearing for "good agent" behavior.

If you're hacking on Sunday's runtime, reading the Hermes source is
recommended — it shows what production-grade tool-calling actually
looks like.
