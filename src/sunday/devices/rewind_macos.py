"""Rewind — continuous screen capture + OCR + searchable history.

Runs on the satellite. Captures the full screen every N seconds via
`screencapture`, OCRs each frame **locally via Apple's Vision framework**
(the same engine Live Text uses), indexes the text in a SQLite FTS5
table. Sunday can then answer "what was on my screen at 2pm" / "find
the slide that mentioned Q3 revenue" by searching the index.

Opt-in by default. Watcher auto-starts on satellite boot only if
~/.sunday/rewind.enabled exists. Toggle via the `rewind_start` tool
(creates the flag + starts the loop) and `rewind_stop` (clears the
flag + cancels). Settings page toggle is the friendlier surface.

Cost: $0. OCR runs locally via a tiny Swift binary that wraps
`VNRecognizeTextRequest`. The binary is built once at first use from
`<repo>/bin/ocr-macos.swift` and cached at `~/.sunday/bin/ocr-macos`.
Requires Xcode Command Line Tools (`xcode-select --install`) for the
one-time build.
"""

from __future__ import annotations

import asyncio
import hashlib
import sqlite3
import subprocess
import time
from pathlib import Path
from typing import Any

import structlog

from sunday.paths import sunday_home

log = structlog.get_logger("sunday.devices.rewind")

REWIND_DIR     = Path("~/.sunday/rewind").expanduser()
REWIND_DB      = Path("~/.sunday/rewind.db").expanduser()
REWIND_FLAG    = Path("~/.sunday/rewind.enabled").expanduser()

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


async def _capture() -> tuple[Path, bytes]:
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
        raw = err.decode("utf-8", errors="replace").strip()
        if "could not create image" in raw.lower() or not raw:
            raise PermissionError(
                "Screen Recording permission not granted to the Sunday satellite. "
                "System Settings → Privacy & Security → Screen Recording → enable it."
            )
        raise RuntimeError(f"screencapture failed: {raw}")
    return path, path.read_bytes()


# ─── OCR via Apple Vision (free + local) ────────────────────────────────


def _ocr_binary_path() -> Path:
    """Compile the tiny Vision wrapper once and cache the binary in
    ~/.sunday/bin/. swiftc ships with Xcode Command Line Tools — if it's
    missing, raise a friendly message."""
    target = sunday_home() / "bin" / "ocr-macos"
    if target.exists():
        return target
    # The .swift source lives at <repo>/bin/ocr-macos.swift; under the
    # editable install it's three levels above this module.
    candidates = [
        Path(__file__).resolve().parents[3] / "bin" / "ocr-macos.swift",
        Path("/opt/sunday/bin/ocr-macos.swift"),  # systemd install layout
    ]
    src = next((c for c in candidates if c.exists()), None)
    if src is None:
        raise FileNotFoundError(f"ocr-macos.swift not found in {candidates}")
    swiftc = "/usr/bin/swiftc"
    if not Path(swiftc).exists():
        raise RuntimeError(
            "swiftc not found. Install Xcode Command Line Tools: `xcode-select --install`"
        )
    target.parent.mkdir(parents=True, exist_ok=True)
    log.info("building rewind ocr binary", src=str(src), target=str(target))
    res = subprocess.run(
        [swiftc, "-O", "-o", str(target), str(src)],
        capture_output=True, text=True, timeout=60,
    )
    if res.returncode != 0:
        raise RuntimeError(f"swiftc failed: {res.stderr.strip() or res.stdout.strip()}")
    return target


async def _ocr(png_path: Path) -> str:
    """Run Apple Vision OCR locally on the captured frame. Returns the
    recognized text, empty string if Vision saw no text."""
    bin_path = _ocr_binary_path()
    proc = await asyncio.create_subprocess_exec(
        str(bin_path), str(png_path),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    out, err = await asyncio.wait_for(proc.communicate(), timeout=15)
    if proc.returncode != 0:
        raise RuntimeError(f"ocr failed: {err.decode(errors='replace').strip()}")
    return out.decode("utf-8", errors="replace").strip()


async def capture_text() -> dict[str, Any]:
    """One-shot: capture the screen right now, OCR it locally via Vision,
    return the text. This is what powers `device_screen_text` — it lets a
    text-only model "see" the screen as readable text without any image
    round-trip or cloud vision cost. The temp PNG is deleted after OCR."""
    tmp = REWIND_DIR / "_oneshot.png"
    tmp.parent.mkdir(parents=True, exist_ok=True)
    proc = await asyncio.create_subprocess_exec(
        "/usr/sbin/screencapture", "-x", "-t", "png", str(tmp),
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.PIPE,
    )
    _out, err = await proc.communicate()
    if proc.returncode != 0:
        raw = err.decode("utf-8", errors="replace").strip()
        if "could not create image" in raw.lower() or not raw:
            return {"error": (
                "SCREEN_RECORDING_DENIED: grant Screen Recording to Sunday in "
                "System Settings → Privacy & Security → Screen Recording."
            )}
        return {"error": f"screencapture failed: {raw}"}
    try:
        text = await _ocr(tmp)
    finally:
        tmp.unlink(missing_ok=True)
    return {"text": text, "chars": len(text)}


# ─── watcher loop ────────────────────────────────────────────────────────


async def watcher_loop(interval: float = DEFAULT_INTERVAL_SECONDS) -> None:
    """Capture → dedupe → OCR → index. Cancellable."""
    global _last_hash
    log.info("rewind watcher starting", interval_s=interval, db=str(REWIND_DB))
    conn = _connect()
    while True:
        try:
            await asyncio.sleep(interval)
            try:
                png_path, png_bytes = await _capture()
            except PermissionError as exc:
                # No point retrying every interval — permission won't appear
                # mid-loop. Stop cleanly so the user grants it and restarts.
                log.warning("rewind stopping: screen recording denied", error=str(exc))
                return
            h = hashlib.sha256(png_bytes).hexdigest()[:HASH_PREFIX_BYTES]
            if h == _last_hash:
                png_path.unlink(missing_ok=True)
                continue
            _last_hash = h
            try:
                ocr_text = await _ocr(png_path)
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
