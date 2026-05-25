# Adapted from hermes/run_agent.py — MIT — (c) 2025 Nous Research
# Ported verbatim from `_repair_tool_call_arguments` and
# `_escape_invalid_chars_in_json_strings` in Hermes (around lines 732-870).
# Real-world LLMs routinely emit broken JSON in tool_call arguments;
# Hermes's repair pass is field-tested across providers we'd otherwise
# have to discover the same bugs for ourselves.

"""Repair malformed tool_call argument JSON.

Strict providers (OpenAI, Anthropic, Mistral) reject any non-conforming
JSON with a 400. Models like GLM-5.1 via Ollama, llama.cpp backends, and
older open-source models routinely emit trailing commas, unescaped
control characters, truncated structures, or Python `None`. This module
attempts cheap repairs in order, returning `"{}"` as a last resort so
the request can still be made (better than crashing the session).
"""

from __future__ import annotations

import json
import re
import structlog

log = structlog.get_logger("sunday.runtime.tool_args")


def _escape_invalid_chars_in_json_strings(raw: str) -> str:
    """Escape unescaped control chars inside JSON string values.

    Walks character-by-character, tracking whether we are inside a
    double-quoted string. Inside strings, replaces literal control
    characters (0x00–0x1F) that aren't already part of an escape
    sequence with their `\\uXXXX` equivalents.
    """
    out: list[str] = []
    in_string = False
    i = 0
    n = len(raw)
    while i < n:
        ch = raw[i]
        if in_string:
            if ch == "\\" and i + 1 < n:
                out.append(ch)
                out.append(raw[i + 1])
                i += 2
                continue
            if ch == '"':
                in_string = False
                out.append(ch)
            elif ord(ch) < 0x20:
                out.append(f"\\u{ord(ch):04x}")
            else:
                out.append(ch)
        else:
            if ch == '"':
                in_string = True
            out.append(ch)
        i += 1
    return "".join(out)


def repair_tool_call_arguments(raw_args: str, tool_name: str = "?") -> str:
    """Attempt to repair malformed tool_call argument JSON.

    Returns a valid JSON object string. If nothing works, returns `"{}"`
    so the request still succeeds — losing the args is better than
    crashing the turn.
    """
    raw_stripped = raw_args.strip() if isinstance(raw_args, str) else ""

    if not raw_stripped:
        log.warning("sanitized empty tool_call arguments", tool=tool_name)
        return "{}"

    if raw_stripped == "None":
        log.warning("sanitized Python-None tool_call arguments", tool=tool_name)
        return "{}"

    # Pass 0: strict=False to accept literal control chars inside strings,
    # then re-serialise into wire-valid JSON. Most-common local-model bug.
    try:
        parsed = json.loads(raw_stripped, strict=False)
        reserialised = json.dumps(parsed, separators=(",", ":"))
        if reserialised != raw_stripped:
            log.warning("repaired unescaped control chars", tool=tool_name)
        return reserialised
    except (json.JSONDecodeError, TypeError, ValueError):
        pass

    fixed = raw_stripped

    # Pass 1: strip trailing commas before } or ]
    fixed = re.sub(r",\s*([}\]])", r"\1", fixed)

    # Pass 2: close unclosed structures
    open_curly = fixed.count("{") - fixed.count("}")
    open_bracket = fixed.count("[") - fixed.count("]")
    if open_curly > 0:
        fixed += "}" * open_curly
    if open_bracket > 0:
        fixed += "]" * open_bracket

    # Pass 3: drop excess trailing closers (bounded)
    for _ in range(50):
        try:
            json.loads(fixed)
            break
        except json.JSONDecodeError:
            if fixed.endswith("}") and fixed.count("}") > fixed.count("{"):
                fixed = fixed[:-1]
            elif fixed.endswith("]") and fixed.count("]") > fixed.count("["):
                fixed = fixed[:-1]
            else:
                break

    try:
        json.loads(fixed)
        log.warning(
            "repaired malformed tool_call arguments",
            tool=tool_name,
            before=raw_stripped[:80],
            after=fixed[:80],
        )
        return fixed
    except json.JSONDecodeError:
        pass

    # Pass 4: escape control chars + retry
    try:
        escaped = _escape_invalid_chars_in_json_strings(fixed)
        if escaped != fixed:
            json.loads(escaped)
            log.warning(
                "repaired control-char-laced tool_call arguments",
                tool=tool_name,
                before=raw_stripped[:80],
                after=escaped[:80],
            )
            return escaped
    except (json.JSONDecodeError, TypeError, ValueError):
        pass

    # Last resort: empty object, but log loudly.
    log.warning(
        "unrepairable tool_call arguments — replacing with empty object",
        tool=tool_name,
        raw=raw_stripped[:200],
    )
    return "{}"
