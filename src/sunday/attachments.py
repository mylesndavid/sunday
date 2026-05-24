"""Attachments — files, images, audio, video on chat messages.

Stored as a list under `metadata['attachments']` on the owning message.
Each entry is the result of `Attachment.to_dict()` so it round-trips through
SQLite JSON cleanly.

Sunday's vision (when the model supports it) reads images directly via
OpenAI multipart content. Other attachment types render as a short text
descriptor — the model can call a read_file / browser_navigate tool to
actually consume them when relevant.
"""

from __future__ import annotations

import base64
import mimetypes
import shutil
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from sunday.paths import sunday_home

IMAGE_MIMES = {"image/png", "image/jpeg", "image/jpg", "image/gif", "image/webp"}


@dataclass(slots=True)
class Attachment:
    id: str
    path: str          # absolute local path
    mime_type: str
    filename: str
    size: int
    url: str | None = None   # remote URL when the source is non-local (e.g. Sendblue)
    kind: str = "file"        # 'image' | 'audio' | 'video' | 'file'

    @classmethod
    def from_local_path(cls, path: str | Path) -> "Attachment":
        p = Path(path).expanduser().resolve()
        if not p.exists():
            raise FileNotFoundError(f"attachment not found: {p}")
        mime, _ = mimetypes.guess_type(p.name)
        mime = mime or "application/octet-stream"
        return cls(
            id=uuid.uuid4().hex[:12],
            path=str(p),
            mime_type=mime,
            filename=p.name,
            size=p.stat().st_size,
            kind=_classify(mime),
        )

    @classmethod
    def from_remote(cls, url: str, mime_type: str | None = None, filename: str | None = None) -> "Attachment":
        mime = mime_type or (mimetypes.guess_type(url)[0]) or "application/octet-stream"
        return cls(
            id=uuid.uuid4().hex[:12],
            path="",
            mime_type=mime,
            filename=filename or url.rsplit("/", 1)[-1],
            size=0,
            url=url,
            kind=_classify(mime),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "path": self.path,
            "mime_type": self.mime_type,
            "filename": self.filename,
            "size": self.size,
            "url": self.url,
            "kind": self.kind,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Attachment":
        return cls(
            id=d.get("id") or uuid.uuid4().hex[:12],
            path=d.get("path") or "",
            mime_type=d.get("mime_type") or "application/octet-stream",
            filename=d.get("filename") or "attachment",
            size=int(d.get("size") or 0),
            url=d.get("url"),
            kind=d.get("kind") or _classify(d.get("mime_type") or ""),
        )

    def is_image(self) -> bool:
        return self.kind == "image" or self.mime_type in IMAGE_MIMES

    def to_llm_image_url(self) -> str | None:
        """Return the URL or data: URI suitable for vision content. None when not an image."""
        if not self.is_image():
            return None
        if self.url:
            return self.url
        if self.path and Path(self.path).exists():
            data = Path(self.path).read_bytes()
            b64 = base64.b64encode(data).decode("ascii")
            return f"data:{self.mime_type};base64,{b64}"
        return None


def _classify(mime: str) -> str:
    if mime.startswith("image/"):
        return "image"
    if mime.startswith("audio/"):
        return "audio"
    if mime.startswith("video/"):
        return "video"
    return "file"


def attachments_dir() -> Path:
    d = sunday_home() / "attachments"
    d.mkdir(parents=True, exist_ok=True)
    return d


def stash_local_file(src: str | Path) -> Attachment:
    """Copy a file into ~/.sunday/attachments/ and return an Attachment record.

    Use when content arrives from a transient source (drag-drop in Electron,
    a CLI --attach pointing at /tmp/...) and we want it durable for future
    LLM context, replay, or re-rendering.
    """
    src_path = Path(src).expanduser().resolve()
    if not src_path.exists():
        raise FileNotFoundError(f"file not found: {src_path}")
    stamp = time.strftime("%Y%m%d-%H%M%S")
    dest = attachments_dir() / f"{stamp}-{src_path.name}"
    shutil.copy2(src_path, dest)
    return Attachment.from_local_path(dest)
