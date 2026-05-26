"""Rewind — continuous screen capture + OCR + searchable history.

Runs on the satellite. Captures the full screen every N seconds via
`screencapture`, OCRs each frame via OpenAI's vision API, indexes the
text in a local SQLite FTS5 table. Sunday can then answer "what was on
my screen at 2pm" / "find the slide that mentioned Q3 revenue" by
searching the index.

Opt-in by default. Watcher auto-starts on satellite boot only if
~/.sunday/rewind.enabled exists. Toggle via the `rewind_start` tool
(creates the flag + starts the loop) and `rewind_stop` (clears the
flag + cancels). Settings page toggle is the friendlier surface.

Cost note: ~$0.002 per frame OCR'd. At the default 5-minute interval
that's roughly $0.50/day during active use. Tune the interval if needed.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import sqlite3
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Any

import httpx
import structlog

log = structlog.get_logger("sunday.devices.rewind")

REWIND_DIR     = Path("~/.sunday/rewind").expanduser()
REWIND_DB      = Path("~/.sunday/rewind.db").expanduser()
REWIND_FLAG    = Path("~/.sunday/rewind.enabled").expanduser()

OCR_MODEL = "gpt-4o-mini"
OCR_PROMPT = (
    "Extract ALL visible text from this screenshot exactly as it appears. "
    "Preserve approximate layout with line breaks. Include UI labels, menu "
    "items, button text. No commentary, no headers, no markdown — just the "
    "raw text. If the screen is empty or shows only an image with no text, "
    "respond with one word: EMPTY"
)

DEFAULT_INTERVAL_SECONDS = 300   # 5 minutes
HASH_PREFIX_BYTES        = 16

_watcher_task: asyncio.Task | None = None
_last_hash: str | None = None


# ─── storage ────────────────────────────────────────────────────────────


def _connect() -> sqlite3.Connection:
    REWIND_DB.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(REWIND_DB, check_same_thread=False)
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS frames (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            ts           REAL NOT NULL,
            image_path   TEXT NOT NULL,
            content_hash TEXT NOT NULL,
            ocr_text     TEXT,
            created_at   REAL NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_frames_ts   ON frames(ts);
        CREATE INDEX IF NOT EXISTS idx_frames_hash ON frames(content_hash);
        """
    )
    try:
        conn.execute(
            "CREATE VIRTUAL TABLE IF NOT EXISTS frames_fts USING fts5("
            "ocr_text, content='frames', content_rowid='id')"
        )
        conn.executescript(
            """
            CREATE TRIGGER IF NOT EXISTS frames_ai AFTER INSERT ON frames BEGIN
              INSERT INTO frames_fts(rowid, ocr_text) VALUES (new.id, new.ocr_text);
            END;
            CREATE TRIGGER IF NOT EXISTS frames_ad AFTER DELETE ON frames BEGIN
              INSERT INTO frames_fts(frames_fts, rowid, ocr_text) VALUES ('delete', old.id, old.ocr_text);
            END;
            """
        )
    except sqlite3.OperationalError:
        log.warning("fts5 not available — rewind search falls back to LIKE")
    conn.commit()
    return conn


# ─── capture + OCR ──────────────────────────────────────────────────────


def is_available() -> bool:
    """We advertise 'rewind' on any macOS satellite that has the
    `screencapture` binary. The actual TCC Screen Recording permission
    is per-process — we can only know at runtime when the user calls
    `rewind_start` whether the binary actually has it. If it doesn't,
    rewind_start surfaces a clear "grant Screen Recording" error and
    nothing is silently lost."""
    import sys
    if sys.platform != "darwin":
        return False
    # /usr/sbin isn't on launchd's default PATH so we can't shutil.which —
    # check the canonical absolute path directly. (Apple has shipped
    # screencapture in /usr/sbin since the dawn of time.)
    return Path("/usr/sbin/screencapture").exists()


async def _capture() -> tuple[bytes, Path]:
    today = time.strftime("%Y-%m-%d")
    when  = time.strftime("%H%M%S")
    day   = REWIND_DIR / today
    day.mkdir(parents=True, exist_ok=True)
    path  = day / f"{when}.png"
    proc = await asyncio.create_subprocess_exec(
        "/usr/sbin/screencapture", "-x", "-t", "png", str(path),
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.PIPE,
    )
    _out, err = await proc.communicate()
    if proc.returncode != 0:
        raise RuntimeError(f"screencapture failed: {err.decode('utf-8', errors='replace').strip()}")
    return path.read_bytes(), path


async def _ocr(png_bytes: bytes) -> str:
    """OCR via OpenAI gpt-4o-mini vision. Cheap enough for periodic use."""
    from sunday.credentials import get_credential
    key = get_credential("OPENAI_API_KEY")
    if not key:
        raise RuntimeError("OPENAI_API_KEY required for Rewind OCR")
    b64 = base64.b64encode(png_bytes).decode("ascii")
    payload = {
        "model": OCR_MODEL,
        "messages": [{
            "role": "user",
            "content": [
                {"type": "text", "text": OCR_PROMPT},
                {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{b64}"}},
            ],
        }],
        "max_tokens": 4000,
        "temperature": 0,
    }
    async with httpx.AsyncClient(timeout=90) as client:
        res = await client.post(
            "https://api.openai.com/v1/chat/completions",
            headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
            json=payload,
        )
    if res.status_code >= 400:
        raise RuntimeError(f"openai {res.status_code}: {res.text[:200]}")
    text = res.json()["choices"][0]["message"]["content"] or ""
    return "" if text.strip() == "EMPTY" else text.strip()


# ─── watcher loop ────────────────────────────────────────────────────────


async def watcher_loop(interval: float = DEFAULT_INTERVAL_SECONDS) -> None:
    """Capture → dedupe → OCR → index. Cancellable."""
    global _last_hash
    log.info("rewind watcher starting", interval_s=interval, db=str(REWIND_DB))
    conn = _connect()
    while True:
        try:
            await asyncio.sleep(interval)
            png_bytes, png_path = await _capture()
            h = hashlib.sha256(png_bytes).hexdigest()[:HASH_PREFIX_BYTES]
            if h == _last_hash:
                png_path.unlink(missing_ok=True)
                continue
            _last_hash = h
            try:
                ocr_text = await _ocr(png_bytes)
            except Exception as exc:  # noqa: BLE001
                log.warning("rewind ocr failed", error=str(exc))
                ocr_text = ""
            ts = time.time()
            conn.execute(
                "INSERT INTO frames (ts, image_path, content_hash, ocr_text, created_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (ts, str(png_path), h, ocr_text, ts),
            )
            conn.commit()
            log.info("rewind frame indexed", hash=h, ocr_chars=len(ocr_text))
        except asyncio.CancelledError:
            log.info("rewind watcher cancelled")
            return
        except Exception as exc:  # noqa: BLE001
            log.warning("rewind iteration failed", error=str(exc))


def is_running() -> bool:
    return _watcher_task is not None and not _watcher_task.done()


def start(interval: float = DEFAULT_INTERVAL_SECONDS) -> dict[str, Any]:
    global _watcher_task
    if is_running():
        return {"already_running": True, "interval_s": interval}
    if not is_available():
        return {"error": "screencapture failed — grant Screen Recording permission to the satellite process"}
    REWIND_FLAG.parent.mkdir(parents=True, exist_ok=True)
    REWIND_FLAG.write_text(str(int(interval)))
    _watcher_task = asyncio.create_task(watcher_loop(interval))
    return {"ok": True, "interval_s": interval}


def stop() -> dict[str, Any]:
    global _watcher_task
    if REWIND_FLAG.exists():
        REWIND_FLAG.unlink()
    if _watcher_task and not _watcher_task.done():
        _watcher_task.cancel()
        _watcher_task = None
        return {"ok": True, "stopped": True}
    return {"already_stopped": True}


def auto_start_if_enabled() -> dict[str, Any]:
    """Called on satellite boot. Starts the watcher only if the flag
    file exists (user previously opted in)."""
    if not REWIND_FLAG.exists():
        return {"ok": True, "enabled": False}
    try:
        interval = float(REWIND_FLAG.read_text().strip() or DEFAULT_INTERVAL_SECONDS)
    except ValueError:
        interval = DEFAULT_INTERVAL_SECONDS
    return start(interval=interval)


# ─── queries ────────────────────────────────────────────────────────────


def _format_row(row: tuple) -> dict[str, Any]:
    return {
        "id":         row[0],
        "ts":         row[1],
        "image_path": row[2],
        "ocr_text":   (row[3] or "")[:1200],
    }


def search(query: str, limit: int = 10) -> list[dict[str, Any]]:
    if not query.strip():
        return []
    conn = _connect()
    try:
        rows = conn.execute(
            """
            SELECT f.id, f.ts, f.image_path, f.ocr_text
            FROM frames_fts
            JOIN frames f ON f.id = frames_fts.rowid
            WHERE frames_fts MATCH ?
            ORDER BY f.ts DESC
            LIMIT ?
            """,
            (query, int(limit)),
        ).fetchall()
    except sqlite3.OperationalError:
        rows = conn.execute(
            "SELECT id, ts, image_path, ocr_text FROM frames "
            "WHERE ocr_text LIKE ? ORDER BY ts DESC LIMIT ?",
            (f"%{query}%", int(limit)),
        ).fetchall()
    return [_format_row(r) for r in rows]


def recent(limit: int = 10) -> list[dict[str, Any]]:
    conn = _connect()
    rows = conn.execute(
        "SELECT id, ts, image_path, ocr_text FROM frames ORDER BY ts DESC LIMIT ?",
        (int(limit),),
    ).fetchall()
    return [_format_row(r) for r in rows]


def stats() -> dict[str, Any]:
    conn = _connect()
    total = conn.execute("SELECT COUNT(*) FROM frames").fetchone()[0]
    if total == 0:
        return {"total": 0, "running": is_running(), "enabled": REWIND_FLAG.exists()}
    oldest, newest = conn.execute(
        "SELECT MIN(ts), MAX(ts) FROM frames"
    ).fetchone()
    return {
        "total":   total,
        "oldest":  oldest,
        "newest":  newest,
        "running": is_running(),
        "enabled": REWIND_FLAG.exists(),
    }
