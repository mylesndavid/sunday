"""Cloudflare Browser Rendering + Sandboxes.

Browser Rendering gives Sunday a headless browser she can drive through
REST. Sandboxes run untrusted code in ephemeral microVMs. Both rendered
via Cloudflare's API so Sunday doesn't need to maintain a local browser
or a sandboxing layer.

Live view / human-in-the-loop: every browser action also broadcasts a
'browser_frame' event over the daemon's WebSocket so the Electron app
(and any other WS viewer) can render what Sunday is currently looking
at and let the user intervene.

Credentials:
  CLOUDFLARE_API_TOKEN     token with Browser Rendering permission
  CLOUDFLARE_ACCOUNT_ID    your account id (or set config.cloudflare.account_id)

Optional, for sandbox_run:
  SANDBOX_WORKER_URL       URL of a Cloudflare Worker exposing your Sandbox
                           runtime. Without it, sandbox_run returns a
                           helpful "deploy a worker to enable" message.
"""

from __future__ import annotations

import base64
import time
import uuid
from pathlib import Path
from typing import Any

import httpx
import structlog

from sunday.attachments import Attachment, attachments_dir
from sunday.config import SundayConfig
from sunday.credentials import get_credential
from sunday.tools import Tool, ToolContext, ToolRegistry

log = structlog.get_logger("sunday.cloud.cloudflare")


def _cf_auth(config: SundayConfig) -> tuple[str, str]:
    token = get_credential("CLOUDFLARE_API_TOKEN")
    account = get_credential("CLOUDFLARE_ACCOUNT_ID") or config.cloudflare.account_id
    if not token:
        raise RuntimeError("CLOUDFLARE_API_TOKEN is missing. Run: sunday credential set CLOUDFLARE_API_TOKEN <token>")
    if not account:
        raise RuntimeError("CLOUDFLARE_ACCOUNT_ID is missing.")
    return token, account


async def _cf_browser_post(
    endpoint: str,
    payload: dict[str, Any],
    config: SundayConfig,
    *,
    expect_binary: bool = False,
) -> Any:
    token, account = _cf_auth(config)
    url = f"{config.cloudflare.api_base}/accounts/{account}/browser-rendering/{endpoint}"
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    async with httpx.AsyncClient(timeout=90) as client:
        res = await client.post(url, headers=headers, json=payload)
    if res.status_code >= 400:
        # Surface CF's error JSON when present
        try:
            err = res.json()
        except ValueError:
            err = {"raw": res.text[:500]}
        raise RuntimeError(f"cloudflare browser-rendering {res.status_code}: {err}")
    if expect_binary:
        return res.content
    try:
        return res.json()
    except ValueError:
        return {"raw": res.text}


# ─── tools ───────────────────────────────────────────────────────────────


_URL_PARAMS = {
    "type": "object",
    "properties": {
        "url": {"type": "string", "description": "Page URL to load."},
        "wait_until": {
            "type": "string",
            "description": "When to consider navigation done. 'load' | 'networkidle0' | 'networkidle2'. Default 'networkidle0'.",
        },
    },
    "required": ["url"],
}

_SCRAPE_PARAMS = {
    "type": "object",
    "properties": {
        "url": {"type": "string", "description": "Page URL."},
        "elements": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "selector": {"type": "string"},
                    "label": {"type": "string"},
                },
                "required": ["selector"],
            },
            "description": "CSS selectors + optional labels. Each is read with innerText.",
        },
    },
    "required": ["url", "elements"],
}

_SANDBOX_PARAMS = {
    "type": "object",
    "properties": {
        "code": {"type": "string", "description": "Source code to execute."},
        "language": {
            "type": "string",
            "description": "'python' | 'javascript' | 'bash'. Default 'python'.",
        },
        "timeout_seconds": {"type": "integer", "description": "Per-execution timeout, default 30."},
    },
    "required": ["code"],
}


async def _broadcast_frame(ctx: ToolContext, url: str, screenshot_path: str | None) -> None:
    """Notify WS viewers about a browser action so they can render a live view."""
    await ctx.broadcast({
        "type": "browser_frame",
        "url": url,
        "screenshot_path": screenshot_path,
        "ts": time.time(),
    })


async def _t_browser_markdown(args: dict[str, Any], ctx: ToolContext) -> Any:
    url = args.get("url")
    if not url:
        return {"error": "'url' is required"}
    payload: dict[str, Any] = {"url": url}
    if args.get("wait_until"):
        payload["waitUntil"] = args["wait_until"]
    try:
        data = await _cf_browser_post("markdown", payload, ctx.config)
    except RuntimeError as exc:
        return {"error": str(exc)}
    # CF returns {"success": true, "result": "<markdown>"}
    md = data.get("result") if isinstance(data, dict) else None
    await _broadcast_frame(ctx, url, None)
    return {"url": url, "markdown": md if isinstance(md, str) else data}


async def _t_browser_screenshot(args: dict[str, Any], ctx: ToolContext) -> Any:
    url = args.get("url")
    if not url:
        return {"error": "'url' is required"}
    payload: dict[str, Any] = {"url": url, "screenshotOptions": {"type": "png"}}
    if args.get("wait_until"):
        payload["waitUntil"] = args["wait_until"]

    try:
        body = await _cf_browser_post("screenshot", payload, ctx.config, expect_binary=True)
    except RuntimeError as exc:
        return {"error": str(exc)}

    # Cloudflare returns the PNG as a base64 string inside a JSON envelope on
    # some plans and as raw bytes on others — handle both.
    if isinstance(body, (bytes, bytearray)) and body[:8] == b"\x89PNG\r\n\x1a\n":
        png = bytes(body)
    else:
        try:
            import json as _json
            parsed = _json.loads(body)
            png = base64.b64decode(parsed.get("result") or parsed.get("screenshot") or "")
        except Exception:  # noqa: BLE001
            return {"error": "could not parse screenshot response"}

    dest = attachments_dir() / f"browser-{uuid.uuid4().hex[:10]}.png"
    dest.write_bytes(png)
    att = Attachment.from_local_path(dest)
    await _broadcast_frame(ctx, url, str(dest))
    return {"url": url, "screenshot_path": str(dest), "attachment": att.to_dict()}


async def _t_browser_scrape(args: dict[str, Any], ctx: ToolContext) -> Any:
    url = args.get("url")
    elements = args.get("elements")
    if not url or not isinstance(elements, list) or not elements:
        return {"error": "'url' and 'elements' (list) are required"}
    payload: dict[str, Any] = {"url": url, "elements": elements}
    try:
        data = await _cf_browser_post("scrape", payload, ctx.config)
    except RuntimeError as exc:
        return {"error": str(exc)}
    await _broadcast_frame(ctx, url, None)
    return data


async def _t_sandbox_run(args: dict[str, Any], ctx: ToolContext) -> Any:
    code = args.get("code")
    language = args.get("language", "python")
    timeout = int(args.get("timeout_seconds") or 30)
    if not code:
        return {"error": "'code' is required"}

    worker_url = get_credential("SANDBOX_WORKER_URL")
    if not worker_url:
        return {
            "error": (
                "SANDBOX_WORKER_URL is not configured. Deploy a Cloudflare Worker "
                "that wraps your Sandbox runtime and set the credential to its URL. "
                "Until then, untrusted code execution is disabled."
            )
        }

    async with httpx.AsyncClient(timeout=timeout + 10) as client:
        res = await client.post(
            worker_url,
            json={"code": code, "language": language, "timeout_seconds": timeout},
        )
    try:
        data = res.json()
    except ValueError:
        data = {"raw": res.text}
    if res.status_code >= 400:
        return {"error": f"sandbox {res.status_code}: {data}"}
    return data


def register(registry: ToolRegistry, config: SundayConfig) -> None:
    registry.register(Tool(
        name="browser_markdown",
        description="Fetch a URL through Cloudflare Browser Rendering and return clean markdown.",
        parameters=_URL_PARAMS,
        run=_t_browser_markdown,
    ))
    registry.register(Tool(
        name="browser_screenshot",
        description=(
            "Take a screenshot of a URL via Cloudflare Browser Rendering. Saves into "
            "~/.sunday/attachments/ and broadcasts a browser_frame event to live-view "
            "WebSocket clients (the Electron app)."
        ),
        parameters=_URL_PARAMS,
        run=_t_browser_screenshot,
    ))
    registry.register(Tool(
        name="browser_scrape",
        description=(
            "Scrape structured data from a URL via Cloudflare Browser Rendering. "
            "Pass `elements` as a list of {selector, label?} entries."
        ),
        parameters=_SCRAPE_PARAMS,
        run=_t_browser_scrape,
    ))
    registry.register(Tool(
        name="sandbox_run",
        description=(
            "Run untrusted code in a Cloudflare Sandbox (via a configured Worker URL). "
            "Returns stdout, stderr, exit code, and runtime."
        ),
        parameters=_SANDBOX_PARAMS,
        run=_t_sandbox_run,
    ))
