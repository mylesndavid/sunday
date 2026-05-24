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

ProviderName = Literal["deepseek", "openai", "anthropic", "offline"]


@dataclass
class ModelConfig:
    """The LLM Sunday speaks with.

    Default is DeepSeek V4 Flash via OpenAI-compatible API — best
    cost-quality point for a conversational personal AI today.
    """
    provider: ProviderName = "deepseek"
    name: str = "deepseek-v4-flash"
    base_url: str = "https://api.deepseek.com/v1"
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
    """Hermes runtime. If the binary is available we use it as Sunday's brain
    (the user wants max leverage from Hermes); otherwise we fall back to a
    direct OpenAI-compatible call."""
    binary: str = "hermes"  # PATH lookup; falls back to ~/.hermes/bin/hermes
    provider: str = "openrouter"  # Hermes' own --provider flag
    model: str = "deepseek/deepseek-chat"  # Hermes' --model flag
    max_turns: int = 1  # we drive the multi-turn loop ourselves
    runtime_choice: Literal["auto", "hermes", "openai"] = "auto"


@dataclass
class CloudflareConfig:
    """Cloudflare Browser Rendering + Sandboxes."""
    account_id: str = ""  # CLOUDFLARE_ACCOUNT_ID env var preferred
    api_base: str = "https://api.cloudflare.com/client/v4"


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

    def ensure_home(self) -> Path:
        self.home.mkdir(parents=True, exist_ok=True)
        return self.home


def load_config() -> SundayConfig:
    """Return the default config.

    YAML overlay loading (~/.sunday/config.yaml) lands in v0.1 when we
    actually have something to configure beyond defaults.
    """
    return SundayConfig()
