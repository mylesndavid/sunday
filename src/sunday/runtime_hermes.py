"""Hermes runtime.

Sunday speaks through the Hermes CLI as a subprocess — the same shape
dcharness uses to drive Agent OS. Hermes is text-in / text-out and
stateless from our perspective: the harness owns the multi-turn loop and
renders the full transcript into one prompt each call.

Tool calling: Hermes' CLI doesn't speak the OpenAI tool_calls JSON spec,
so we ask the model (via a protocol suffix on the system prompt) to emit
fenced ```tool blocks. The harness parses them, executes the tool, and
feeds the result back as an additional user-turn for the next Hermes
call. Same loop pattern, different wire format.
"""

from __future__ import annotations

import asyncio
import json
import re
import uuid
from typing import Any

import structlog

from sunday.config import SundayConfig
from sunday.runtime import CompletionResult, DeltaHandler, Runtime, ToolCall

log = structlog.get_logger("sunday.runtime.hermes")


HERMES_TOOL_PROTOCOL = """

# Tool protocol

When you need to use a tool, emit exactly one fenced code block tagged `tool` containing one JSON object, then stop:

```tool
{"name": "tool_name", "arguments": {"...": "..."}}
```

The harness will run the tool and reply with the result as an additional user turn. You can call more tools or answer the user. Never invent tool names.

Available tools:
{tools_listing}
"""

TOOL_BLOCK_RE = re.compile(r"```tool\s*\n(.*?)\n```", re.DOTALL)


class HermesRuntime:
    name = "hermes"

    def __init__(self, config: SundayConfig, binary_path: str) -> None:
        self.config = config
        self.binary_path = binary_path

    async def complete(
        self,
        *,
        system_prompt: str,
        messages: list[dict[str, Any]],
        tools_schema: list[dict[str, Any]] | None,
        on_delta: DeltaHandler | None = None,
    ) -> CompletionResult:
        full_system = system_prompt
        if tools_schema:
            tools_listing = "\n".join(
                f"- {t['function']['name']}: {t['function'].get('description', '')}"
                for t in tools_schema
            )
            # `.replace` instead of `.format` because the protocol template
            # contains literal `{"name": ...}` JSON braces inside the fenced
            # ```tool``` example — `.format` would treat them as placeholders.
            full_system = system_prompt + HERMES_TOOL_PROTOCOL.replace(
                "{tools_listing}", tools_listing
            )

        prompt = _render_transcript(messages)

        cmd = [
            self.binary_path,
            "chat",
            "-Q",                                                # quiet output
            "--provider", self.config.hermes.provider,
            "-m", self.config.hermes.model,
            "--max-turns", str(self.config.hermes.max_turns),
            "--source", "sunday",
            "-t", "",                                            # disable hermes built-in tools
            "-q", f"{full_system}\n\n{prompt}",
        ]

        log.debug("hermes invoke", model=self.config.hermes.model, prompt_chars=len(prompt))
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout_bytes, stderr_bytes = await proc.communicate()
        stdout = stdout_bytes.decode("utf-8", errors="replace")
        stderr = stderr_bytes.decode("utf-8", errors="replace")

        if proc.returncode != 0:
            raise RuntimeError(
                f"hermes exited {proc.returncode}: {stderr.strip() or stdout.strip()}"
            )

        content = _strip_hermes_chrome(stdout)
        tool_calls = _extract_tool_calls(content)
        # If tool calls were emitted, strip them from the visible content so
        # the chat log doesn't carry duplicate tool-call JSON.
        visible = TOOL_BLOCK_RE.sub("", content).strip()

        # Hermes CLI doesn't expose token-level streaming. Emit one delta at
        # the end so callers that wired on_delta still see the content land.
        if on_delta is not None and visible:
            await on_delta(visible)

        return CompletionResult(
            content=visible,
            tool_calls=tool_calls,
            finish_reason="tool_calls" if tool_calls else "stop",
            raw={"runtime": "hermes", "model": self.config.hermes.model},
        )


def _render_transcript(messages: list[dict[str, Any]]) -> str:
    """Flatten the message list into a single Hermes prompt.

    Tool messages get rendered as a `Tool result` block so the model has the
    full chain of work to reason over.
    """
    lines: list[str] = []
    for msg in messages:
        role = msg.get("role", "user")
        if role == "tool":
            label = f"Tool result (id={msg.get('tool_call_id', '?')})"
            lines.append(f"{label}:\n{msg.get('content', '')}")
            continue
        if role == "assistant":
            tcs = msg.get("tool_calls") or []
            if tcs:
                rendered_calls = "\n".join(
                    f"```tool\n{json.dumps({'name': tc['function']['name'], 'arguments': tc['function']['arguments']})}\n```"
                    for tc in tcs
                )
                lines.append(f"Assistant:\n{msg.get('content', '') or ''}\n{rendered_calls}")
                continue
            lines.append(f"Assistant:\n{msg.get('content', '')}")
            continue
        if role == "user":
            lines.append(f"User:\n{msg.get('content', '')}")
            continue
        lines.append(f"{role.title()}:\n{msg.get('content', '')}")
    lines.append("Assistant:")
    return "\n\n".join(lines)


def _strip_hermes_chrome(raw: str) -> str:
    """Hermes CLI prints decorative box characters around its output. Drop them."""
    keep: list[str] = []
    for line in raw.splitlines():
        stripped = line.strip()
        if not stripped:
            keep.append(line.rstrip())
            continue
        if stripped.startswith("session_id:"):
            continue
        if stripped.startswith(("╭", "╰", "│", "┊", "↻")):
            continue
        if re.match(r"^[╭╰─│⚕\s]+(Hermes)?[\s]*$", stripped):
            continue
        keep.append(line.rstrip())
    return "\n".join(keep).strip()


def _extract_tool_calls(content: str) -> list[ToolCall]:
    """Pull out fenced ```tool``` blocks. Each becomes a ToolCall."""
    out: list[ToolCall] = []
    for match in TOOL_BLOCK_RE.finditer(content):
        block = match.group(1).strip()
        try:
            payload = json.loads(block)
        except json.JSONDecodeError as exc:
            log.warning("invalid tool block", error=str(exc), block=block[:200])
            continue
        if not isinstance(payload, dict):
            continue
        name = payload.get("name")
        if not isinstance(name, str):
            continue
        args = payload.get("arguments", {})
        out.append(
            ToolCall(
                id=f"hermes_{uuid.uuid4().hex[:12]}",
                name=name,
                arguments=json.dumps(args) if not isinstance(args, str) else args,
            )
        )
    return out
