"""Provider adapters — one file per backend.

The contract is `Provider.complete(*, system_prompt, messages, tools_schema,
on_delta) -> CompletionResult`. Each provider knows how to:
  - call its specific HTTP API
  - emit streaming content deltas through on_delta as they arrive
  - assemble tool_call deltas (indexed) into a final list
  - normalize the response into Sunday's CompletionResult

Hermes pattern: providers are self-contained — the agent loop in core.py
doesn't know or care which one is running. Pick yours via config.
"""
