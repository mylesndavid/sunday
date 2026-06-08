# Adapted from hermes/run_agent.py — MIT — (c) 2025 Nous Research
# Ported verbatim from:
#   _SURROGATE_RE                 (run_agent.py:465)
#   _sanitize_surrogates          (run_agent.py:616)
#   _sanitize_structure_surrogates (run_agent.py:631)
#   _sanitize_messages_surrogates (run_agent.py:664)
#   AIAgent._repair_message_sequence (run_agent.py:4464)
# These run on every API call as defensive belts. Byte-level reasoning
# models (mimo, kimi, glm) emit lone surrogate code points that crash
# json.dumps inside the OpenAI SDK; session resumption can leave
# malformed role alternation that triggers silent empty responses on
# every provider. Hermes already learned this the expensive way.

"""Message-sequence + unicode hardening for outgoing LLM calls.

Two defensive passes that run before every provider call in brain.py:

  - sanitize_messages_surrogates: walks the messages list in place,
    replaces lone surrogates with U+FFFD. Prevents json.dumps crashes
    in the OpenAI SDK that some byte-level reasoning models trigger.

  - repair_message_sequence: drops stray tool messages that don't
    follow a known assistant tool_call_id, and merges consecutive
    user messages. Prevents silent-empty-response loops from strict
    providers (OpenAI, OpenRouter, Anthropic).
"""

from __future__ import annotations

import re
from typing import Any

_SURROGATE_RE = re.compile(r"[\ud800-\udfff]")


def sanitize_surrogates(text: str) -> str:
    """Replace lone surrogate code points with U+FFFD (replacement char)."""
    if _SURROGATE_RE.search(text):
        return _SURROGATE_RE.sub("�", text)
    return text


def _sanitize_structure_surrogates(payload: Any) -> bool:
    """Walk nested dict/list payloads in place; replace surrogates. Returns
    True if any were replaced."""
    found = False

    def _walk(node: Any) -> None:
        nonlocal found
        if isinstance(node, dict):
            for key, value in node.items():
                if isinstance(value, str):
                    if _SURROGATE_RE.search(value):
                        node[key] = _SURROGATE_RE.sub("�", value)
                        found = True
                elif isinstance(value, (dict, list)):
                    _walk(value)
        elif isinstance(node, list):
            for idx, value in enumerate(node):
                if isinstance(value, str):
                    if _SURROGATE_RE.search(value):
                        node[idx] = _SURROGATE_RE.sub("�", value)
                        found = True
                elif isinstance(value, (dict, list)):
                    _walk(value)

    _walk(payload)
    return found


def sanitize_messages_surrogates(messages: list) -> bool:
    """Replace surrogates in every string field of every message, in place.

    Covers `content` (str + multimodal list), `name`, tool_call ids /
    names / arguments, plus any other string or nested field on the
    message (reasoning, reasoning_content, reasoning_details, ...).
    """
    found = False
    for msg in messages:
        if not isinstance(msg, dict):
            continue
        content = msg.get("content")
        if isinstance(content, str) and _SURROGATE_RE.search(content):
            msg["content"] = _SURROGATE_RE.sub("�", content)
            found = True
        elif isinstance(content, list):
            for part in content:
                if isinstance(part, dict):
                    text = part.get("text")
                    if isinstance(text, str) and _SURROGATE_RE.search(text):
                        part["text"] = _SURROGATE_RE.sub("�", text)
                        found = True
        name = msg.get("name")
        if isinstance(name, str) and _SURROGATE_RE.search(name):
            msg["name"] = _SURROGATE_RE.sub("�", name)
            found = True
        tool_calls = msg.get("tool_calls")
        if isinstance(tool_calls, list):
            for tc in tool_calls:
                if not isinstance(tc, dict):
                    continue
                tc_id = tc.get("id")
                if isinstance(tc_id, str) and _SURROGATE_RE.search(tc_id):
                    tc["id"] = _SURROGATE_RE.sub("�", tc_id)
                    found = True
                fn = tc.get("function")
                if isinstance(fn, dict):
                    fn_name = fn.get("name")
                    if isinstance(fn_name, str) and _SURROGATE_RE.search(fn_name):
                        fn["name"] = _SURROGATE_RE.sub("�", fn_name)
                        found = True
                    fn_args = fn.get("arguments")
                    if isinstance(fn_args, str) and _SURROGATE_RE.search(fn_args):
                        fn["arguments"] = _SURROGATE_RE.sub("�", fn_args)
                        found = True
        for key, value in msg.items():
            if key in {"content", "name", "tool_calls", "role"}:
                continue
            if isinstance(value, str):
                if _SURROGATE_RE.search(value):
                    msg[key] = _SURROGATE_RE.sub("�", value)
                    found = True
            elif isinstance(value, (dict, list)):
                if _sanitize_structure_surrogates(value):
                    found = True
    return found


def repair_message_sequence(messages: list) -> int:
    """Collapse malformed role-alternation in the live history. Returns
    repair count.

    - Heals an assistant message whose `tool_calls` were never answered —
      a daemon crash mid-turn leaves tool_calls with no following tool
      results, which providers 400 ("tool_calls must be followed by tool
      messages"), wedging EVERY future turn until the bad message ages out.
      Strips the unanswered tool_calls (keeping any text); drops the message
      if nothing's left.
    - Drops stray `tool` messages whose tool_call_id doesn't match any
      preceding assistant tool_call.
    - Merges consecutive `user` messages with a blank line separator
      so no user input is lost.
    """
    if not messages:
        return 0

    repairs = 0

    # Pass 0: heal assistant messages with unanswered tool_calls. The tool
    # results for an assistant(tool_calls) message arrive as the run of `tool`
    # messages immediately after it; if any required tool_call_id is missing
    # from that run, the request is malformed (crash mid-turn). Strip the
    # tool_calls so the turn can proceed; Pass 1 then sweeps any now-orphaned
    # tool results.
    healed: list[Any] = []
    i, n = 0, len(messages)
    while i < n:
        msg = messages[i]
        if isinstance(msg, dict) and msg.get("role") == "assistant" and msg.get("tool_calls"):
            answered: set[str] = set()
            j = i + 1
            while j < n and isinstance(messages[j], dict) and messages[j].get("role") == "tool":
                tid = messages[j].get("tool_call_id")
                if tid:
                    answered.add(tid)
                j += 1
            required = {
                tc.get("id") for tc in msg["tool_calls"]
                if isinstance(tc, dict) and tc.get("id")
            }
            if not required.issubset(answered):
                repairs += 1
                stripped = {k: v for k, v in msg.items() if k != "tool_calls"}
                content = stripped.get("content")
                if (isinstance(content, str) and content.strip()) or (isinstance(content, list) and content):
                    healed.append(stripped)
                # else: assistant had nothing but the dead tool_calls — drop it
                i += 1
                continue
        healed.append(msg)
        i += 1
    if repairs:
        messages[:] = healed

    # Pass 1: drop stray tool messages without a matching assistant
    known_tool_ids: set[str] = set()
    filtered: list[Any] = []
    for msg in messages:
        if not isinstance(msg, dict):
            filtered.append(msg)
            continue
        role = msg.get("role")
        if role == "assistant":
            known_tool_ids = set()
            for tc in (msg.get("tool_calls") or []):
                tc_id = tc.get("id") if isinstance(tc, dict) else None
                if tc_id:
                    known_tool_ids.add(tc_id)
            filtered.append(msg)
        elif role == "tool":
            tc_id = msg.get("tool_call_id")
            if tc_id and tc_id in known_tool_ids:
                filtered.append(msg)
            else:
                repairs += 1
        else:
            if role == "user":
                known_tool_ids = set()
            filtered.append(msg)

    # Pass 2: merge consecutive user messages (text only — preserve multimodal)
    merged: list[Any] = []
    for msg in filtered:
        if (
            merged
            and isinstance(msg, dict)
            and msg.get("role") == "user"
            and isinstance(merged[-1], dict)
            and merged[-1].get("role") == "user"
        ):
            prev = merged[-1]
            prev_content = prev.get("content", "")
            new_content = msg.get("content", "")
            if isinstance(prev_content, str) and isinstance(new_content, str):
                prev["content"] = (
                    (prev_content + "\n\n" + new_content)
                    if prev_content and new_content
                    else (prev_content or new_content)
                )
                repairs += 1
                continue
        merged.append(msg)

    if repairs > 0:
        messages[:] = merged

    return repairs
