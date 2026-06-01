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

ProviderName = Literal["openrouter", "openai", "anthropic", "deepseek-direct", "codex", "offline"]


@dataclass
class ModelConfig:
    """The LLM Sunday speaks with.

    Default is a DeepSeek model accessed *via OpenRouter* — same wire format
    everything else uses (OpenAI-compatible chat completions), and OpenRouter
    handles provider routing + failover for us. Override `name` with any
    OpenRouter slug to swap models without touching code.

    Use 'deepseek-direct' only if you specifically want to hit DeepSeek's
    own API instead of going through OpenRouter.
    """
    provider: ProviderName = "openrouter"
    name: str = "deepseek/deepseek-v4-flash"
    base_url: str = "https://openrouter.ai/api/v1"
    # Reasoning costs latency but cleans up multi-step thinking and tool routing.
    # On by default; turn off for ambient conversational replies if it becomes a problem.
    reasoning: bool = True


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
    """Voice I/O. Realtime model used for full-duplex conversation."""
    realtime_model: str = "gpt-realtime"
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
    """VAPI outbound voice calls."""
    api_base: str = "https://api.vapi.ai"
    model_provider: str = "openai"
    model_name: str = "gpt-4o-mini"
    voice_provider: str = "11labs"
    voice_id: str = "rachel"
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
    return cfg
