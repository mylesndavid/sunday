"""Sunday's configuration.

Source of truth for the model Sunday speaks with, where her memory of you
lives, and a few feature flags. Loaded from ~/.sunday/config.yaml when
present; otherwise the defaults below.

Kept deliberately simple — dataclasses, no Pydantic — so reading this file
tells you everything Sunday is configured to do.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

ProviderName = Literal["openrouter", "openai", "anthropic", "deepseek-direct", "codex", "ollama", "offline"]


@dataclass
class ModelConfig:
    """The LLM Sunday's brain speaks with.

    Default is OpenAI's gpt-5.5 hit directly — a frontier reasoning model, so
    replies actually think before they land. The runtime router (see
    runtime/router.py) auto-fails-over to any other credentialed provider on
    402 / rate-limit; on a box logged into the codex CLI that means the next
    stop is Codex/gpt-5.x (your ChatGPT subscription, no key) — also a real
    reasoning model. Background fact-extraction runs on a cheap utility model
    (gpt-4o-mini) separately; see runtime.build_utility_runtime. The brain
    itself never lands on gpt-4o-mini.

    Override `name`/`provider` to swap models. Use 'openrouter' (with an
    OPENROUTER_API_KEY) for unified routing across vendors, 'codex' to make
    the ChatGPT subscription primary, or 'ollama' for a local model.
    """
    provider: ProviderName = "openai"
    name: str = "gpt-5.5"
    base_url: str = "https://api.openai.com/v1"
    # Reasoning costs latency but cleans up multi-step thinking and tool routing.
    # On by default; turn off for ambient conversational replies if it becomes a problem.
    reasoning: bool = True
    # OpenRouter-only provider pin (ignored unless base_url is OpenRouter). Pins
    # the routing order to tame the TTFT tail when a model is served by many
    # backends of varying speed. Empty -> sort:latency, which is correct for the
    # default direct-OpenAI path and any non-OpenRouter model.
    providers: list[str] = field(default_factory=list)
    allow_fallbacks: bool = False


@dataclass
class MemoryConfig:
    """Where Sunday's memory of the user lives.

    SQLite + sqlite-vec for vector search — zero infrastructure, fully local.
    """
    db_path: Path = field(default_factory=lambda: Path.home() / ".sunday" / "memory.db")
    embed_model: str = "text-embedding-3-small"
    embed_dims: int = 1536
    top_k: int = 8


@dataclass
class VoiceConfig:
    """Voice I/O. Realtime model used for full-duplex conversation.
    `provider` picks the realtime backend: "openai" (gpt-realtime, WebRTC) or
    "gemini" (Gemini Live, WebSocket). Both speak Sunday's tools."""
    provider: str = "openai"
    realtime_model: str = "gpt-realtime-2"
    realtime_gemini_model: str = "gemini-2.5-flash-native-audio-preview"
    tts_model: str = "gpt-4o-mini-tts"
    tts_voice: str = "alloy"
    stt_model: str = "whisper-1"


@dataclass
class ServerConfig:
    """HTTP + WebSocket server. The daemon binds local-only by default."""
    host: str = "127.0.0.1"
    port: int = 8765


@dataclass
class HermesConfig:
    """Legacy: kept for any external integration that wants to subprocess the
    Hermes CLI directly. Sunday's core runtime is now a native fork of
    Hermes's loop (see src/sunday/runtime/), so this is no longer the
    default brain. Only used by the `delegate_to_hermes` sub-agent tool,
    which still subprocesses an installed `hermes` binary when present."""
    binary: str = "hermes"
    provider: str = "openrouter"
    model: str = "deepseek/deepseek-chat"
    max_turns: int = 1


@dataclass
class CloudflareConfig:
    """Cloudflare Browser Rendering + Sandboxes."""
    account_id: str = ""  # CLOUDFLARE_ACCOUNT_ID env var preferred
    api_base: str = "https://api.cloudflare.com/client/v4"


@dataclass
class VapiConfig:
    """VAPI outbound voice calls.

    The on-call brain is a separate, mid-tier model that runs *inside* VAPI's
    voice pipeline (not Sunday's own router) — it has to be one VAPI supports.
    gpt-4o is the default: stronger than gpt-4o-mini at staying on-objective and
    handling phone-tree fumbles, and confirmed VAPI-supported. Don't point this
    at gpt-5.x — not confirmed on VAPI. Overridable.
    """
    api_base: str = "https://api.vapi.ai"
    model_provider: str = "openai"
    model_name: str = "gpt-4o"
    voice_provider: str = "vapi"
    voice_id: str = "Elliot"
    transcriber_provider: str = "deepgram"
    transcriber_model: str = "nova-2"
    first_message: str = "Hi, this is Sunday calling on behalf of my user. Got a second?"


@dataclass
class SundayConfig:
    """Root config."""
    home: Path = field(default_factory=lambda: Path.home() / ".sunday")
    model: ModelConfig = field(default_factory=ModelConfig)
    memory: MemoryConfig = field(default_factory=MemoryConfig)
    voice: VoiceConfig = field(default_factory=VoiceConfig)
    server: ServerConfig = field(default_factory=ServerConfig)
    hermes: HermesConfig = field(default_factory=HermesConfig)
    cloudflare: CloudflareConfig = field(default_factory=CloudflareConfig)
    vapi: VapiConfig = field(default_factory=VapiConfig)
    # Native iMessage channel: when on, the daemon watches this machine's own
    # Messages chat.db for inbound and replies via AppleScript — no Sendblue,
    # no send queue. Off by default; intended for the dedicated "Sunday"
    # macOS-user/Apple-ID box. Enable via SUNDAY_IMESSAGE_NATIVE=1. Keep this
    # OR Sendblue answering, never both on the same number/account.
    imessage_native: bool = False
    # Real-app typing + read receipts for the native channel. AppleScript `send`
    # is headless (no typing/read), so these are produced by GUI-driving the
    # actual Messages app — which only works when the Sunday account is the
    # FOREGROUND/displayed session (a background fast-user-switched session never
    # renders the window, so it's a no-op there). Off by default; strictly
    # best-effort — a failure never blocks the reply. Enable on the dedicated
    # always-on Mac (where Sunday is the logged-in user) via
    # SUNDAY_IMESSAGE_INDICATORS=1.
    imessage_indicators: bool = False

    def ensure_home(self) -> Path:
        self.home.mkdir(parents=True, exist_ok=True)
        return self.home


def load_config() -> SundayConfig:
    """Return the default config, with a couple of env overrides.

    SUNDAY_HOST / SUNDAY_PORT let the daemon bind somewhere other than
    127.0.0.1:8765 — needed when it runs in a container (bind 0.0.0.0).
    YAML overlay loading (~/.sunday/config.yaml) lands later.
    """
    import os
    cfg = SundayConfig()
    host = os.environ.get("SUNDAY_HOST")
    port = os.environ.get("SUNDAY_PORT")
    if host:
        cfg.server.host = host
    if port:
        try:
            cfg.server.port = int(port)
        except ValueError:
            pass
    if os.environ.get("SUNDAY_IMESSAGE_NATIVE", "").strip().lower() in ("1", "true", "yes", "on"):
        cfg.imessage_native = True
    if os.environ.get("SUNDAY_IMESSAGE_INDICATORS", "").strip().lower() in ("1", "true", "yes", "on"):
        cfg.imessage_indicators = True
    return cfg
