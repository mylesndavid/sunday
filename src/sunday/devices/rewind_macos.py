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
import re
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
RETENTION_DAYS           = 3     # keep ~last few days of frames; older get pruned

# Frame footprint controls. A raw retina screencapture JPEG is ~1.2 MB; OCR
# does NOT need retina resolution. Downscaling the longest edge to MAX_EDGE_PX
# and recompressing at JPEG_QUALITY cuts each frame ~8× (measured: 1227 KB →
# 157 KB) while keeping menu-bar/app/window text readable. At ~212 frames/day
# that's ~33 MB/day, ~100 MB at 3-day retention — vs ~254 MB/day before.
MAX_EDGE_PX  = 1440   # longest edge after downscale (sips -Z)
JPEG_QUALITY = 40     # sips -s formatOptions (0–100); 40 stays OCR-legible
# Hard backstop: even if per-frame assumptions change, the rewind dir total can
# never exceed this. _prune() deletes oldest frames until under it.
MAX_TOTAL_MB = 400

_watcher_task: asyncio.Task | None = None
_last_hash: str | None = None


# ─── storage ────────────────────────────────────────────────────────────

# Richer per-frame metadata the Timeline layer segments/summarizes on. Old
# rewind DBs predate these; _ensure_frame_columns adds any that are missing on
# every connect so both the rewind watcher and timeline_macos see one schema.
_FRAME_EXTRA_COLUMNS = {
    "active_app":       "TEXT",
    "window_title":     "TEXT",
    "browser_url":      "TEXT",
    "thumbnail_path":   "TEXT",
    "privacy_redacted": "INTEGER DEFAULT 0",
}


def _ensure_frame_columns(conn: sqlite3.Connection) -> None:
    """Idempotently add the Timeline metadata columns. ALTER TABLE ADD COLUMN
    can't be `IF NOT EXISTS`, so we diff against PRAGMA table_info and only add
    what's absent — safe to run on every connect."""
    have = {r[1] for r in conn.execute("PRAGMA table_info(frames)")}
    for col, decl in _FRAME_EXTRA_COLUMNS.items():
        if col not in have:
            conn.execute(f"ALTER TABLE frames ADD COLUMN {col} {decl}")


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
    _ensure_frame_columns(conn)
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
    path  = day / f"{when}.jpg"
    proc = await asyncio.create_subprocess_exec(
        # JPEG, not PNG — a full-screen PNG is ~2 MB; JPEG is ~10x smaller and
        # fine for OCR + thumbnails. (Was the main reason rewind ballooned.)
        "/usr/sbin/screencapture", "-x", "-t", "jpg", str(path),
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
    await _downscale(path)
    return path, path.read_bytes()


async def _downscale(path: Path) -> None:
    """Shrink the just-captured JPEG in place: longest edge → MAX_EDGE_PX,
    quality → JPEG_QUALITY, via macOS `sips`. This is the main footprint
    lever (~8× smaller) and OCR still reads the result fine. If `sips` is
    missing or fails, we leave the original frame untouched — never lose the
    capture, never crash the watcher."""
    try:
        proc = await asyncio.create_subprocess_exec(
            "/usr/bin/sips",
            "-Z", str(MAX_EDGE_PX),
            "-s", "formatOptions", str(JPEG_QUALITY),
            str(path),
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )
        _out, err = await asyncio.wait_for(proc.communicate(), timeout=15)
        if proc.returncode != 0:
            log.warning(
                "rewind downscale failed; keeping full-size frame",
                error=err.decode("utf-8", errors="replace").strip(),
            )
    except Exception as exc:  # noqa: BLE001 — never let resizing kill a capture
        log.warning("rewind downscale errored; keeping full-size frame", error=str(exc))


# ─── active-window context (best-effort, for the Timeline layer) ─────────


def _frontinfo_binary_path() -> Path:
    """Compile the tiny NSWorkspace/CGWindowList helper once and cache it in
    ~/.sunday/bin/frontinfo — same compile-once pattern as the OCR binary. Raises
    if swiftc or the source is missing; the caller treats that as "no rich
    context" and falls back to lsappinfo."""
    target = sunday_home() / "bin" / "frontinfo"
    if target.exists():
        return target
    candidates = [
        Path(__file__).resolve().parents[3] / "bin" / "frontinfo-macos.swift",
        Path("/opt/sunday/bin/frontinfo-macos.swift"),
    ]
    src = next((c for c in candidates if c.exists()), None)
    if src is None:
        raise FileNotFoundError(f"frontinfo-macos.swift not found in {candidates}")
    swiftc = "/usr/bin/swiftc"
    if not Path(swiftc).exists():
        raise RuntimeError("swiftc not found (install Xcode Command Line Tools)")
    target.parent.mkdir(parents=True, exist_ok=True)
    log.info("building rewind frontinfo binary", src=str(src), target=str(target))
    res = subprocess.run(
        [swiftc, "-O", "-o", str(target), str(src)],
        capture_output=True, text=True, timeout=90,
    )
    if res.returncode != 0:
        raise RuntimeError(f"swiftc failed: {res.stderr.strip() or res.stdout.strip()}")
    return target


async def _lsappinfo_front() -> str:
    """Frontmost app display name via Launch Services — zero dependencies, no
    permission, no hang. The fallback when the Swift helper isn't available."""
    p1 = await asyncio.create_subprocess_exec(
        "/usr/bin/lsappinfo", "front",
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
    )
    asn_out, _ = await asyncio.wait_for(p1.communicate(), timeout=3)
    asn = asn_out.decode("utf-8", errors="replace").strip()
    if not asn:
        return ""
    p2 = await asyncio.create_subprocess_exec(
        "/usr/bin/lsappinfo", "info", "-only", "name", asn,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
    )
    name_out, _ = await asyncio.wait_for(p2.communicate(), timeout=3)
    m = re.search(r'"LSDisplayName"="(.*)"', name_out.decode("utf-8", errors="replace"))
    return (m.group(1) if m else "").strip()


async def _active_context() -> tuple[str, str]:
    """Frontmost app name + focused window title — WITHOUT osascript/System Events.

    The old System Events path hung for 5s+ every frame: querying "first
    application process whose frontmost is true" needs Automation permission, and
    in a background/headless session macOS blocks waiting on a consent prompt that
    never shows, so it always timed out to ('', '') — the app=None bug.

    Primary: a compiled Swift helper (NSWorkspace for the app — no permission;
    CGWindowList for the title — reuses the Screen Recording permission capture
    already holds). Fallback: `lsappinfo` for the app name alone. Never hangs
    (short timeouts), never raises — a bad probe just yields ('', '')."""
    try:
        bin_path = _frontinfo_binary_path()
        proc = await asyncio.create_subprocess_exec(
            str(bin_path),
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
        )
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=3)
        if proc.returncode == 0:
            app, _sep, title = out.decode("utf-8", errors="replace").strip().partition("\t")
            if app.strip():
                return app.strip(), title.strip()
    except Exception as exc:  # noqa: BLE001 — fall through to lsappinfo
        log.debug("frontinfo helper unavailable, using lsappinfo", error=str(exc))
    try:
        return await _lsappinfo_front(), ""
    except Exception:  # noqa: BLE001 — context is a nice-to-have, never fatal
        return "", ""


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


def _dir_total_bytes() -> int:
    """Total bytes of all JPEG frames under REWIND_DIR (recursively). Cheap
    enough to call each prune; ignores the DB file itself."""
    total = 0
    if not REWIND_DIR.exists():
        return 0
    for f in REWIND_DIR.rglob("*.jpg"):
        try:
            total += f.stat().st_size
        except OSError:
            pass
    return total


def _prune_to_size_cap(conn: sqlite3.Connection) -> None:
    """Backstop prune: if the rewind dir total exceeds MAX_TOTAL_MB, delete the
    oldest frames (image file + its DB row) until back under the cap. Belt-and-
    suspenders alongside the time-based prune — guarantees the footprint can
    never run away even if per-frame size assumptions change."""
    import shutil
    cap = MAX_TOTAL_MB * 1024 * 1024
    total = _dir_total_bytes()
    if total <= cap:
        return
    # Walk indexed frames oldest-first, deleting until under the cap.
    rows = conn.execute("SELECT id, image_path FROM frames ORDER BY ts ASC").fetchall()
    deleted_ids: list[int] = []
    for fid, p in rows:
        if total <= cap:
            break
        try:
            sz = Path(p).stat().st_size
        except OSError:
            sz = 0
        try:
            Path(p).unlink(missing_ok=True)
        except Exception:  # noqa: BLE001
            pass
        deleted_ids.append(fid)
        total -= sz
    if deleted_ids:
        conn.executemany("DELETE FROM frames WHERE id = ?", [(i,) for i in deleted_ids])
        conn.commit()
        log.info("rewind size-cap prune", removed=len(deleted_ids), under_mb=MAX_TOTAL_MB)
    # Still over cap with no indexed frames left to drop? Sweep oldest day-folders
    # of orphan (captured-but-unindexed) frames until under the cap.
    if total > cap and REWIND_DIR.exists():
        days = sorted(
            d for d in REWIND_DIR.iterdir()
            if d.is_dir() and len(d.name) == 10
        )
        for d in days:
            if _dir_total_bytes() <= cap:
                break
            shutil.rmtree(d, ignore_errors=True)


def _prune(conn: sqlite3.Connection) -> None:
    """Drop frames older than RETENTION_DAYS — both the image files and their
    index rows — so rewind stays bounded instead of growing forever. Then apply
    the MAX_TOTAL_MB size cap as a hard backstop."""
    import shutil
    cutoff = time.time() - RETENTION_DAYS * 86400
    try:
        rows = conn.execute("SELECT image_path FROM frames WHERE ts < ?", (cutoff,)).fetchall()
        for (p,) in rows:
            try: Path(p).unlink(missing_ok=True)
            except Exception: pass  # noqa: BLE001
        if rows:
            conn.execute("DELETE FROM frames WHERE ts < ?", (cutoff,))
            conn.commit()
        # Sweep whole day-folders older than the window (catches orphan files
        # that were captured but never indexed). YYYY-MM-DD sorts chronologically.
        cutoff_day = time.strftime("%Y-%m-%d", time.localtime(cutoff))
        for d in REWIND_DIR.iterdir():
            if d.is_dir() and len(d.name) == 10 and d.name < cutoff_day:
                shutil.rmtree(d, ignore_errors=True)
    except Exception as exc:  # noqa: BLE001
        log.warning("rewind prune failed", error=str(exc))
    # Hard backstop independent of time: keep total footprint under MAX_TOTAL_MB.
    try:
        _prune_to_size_cap(conn)
    except Exception as exc:  # noqa: BLE001
        log.warning("rewind size-cap prune failed", error=str(exc))


async def watcher_loop(interval: float = DEFAULT_INTERVAL_SECONDS) -> None:
    """Capture → dedupe → OCR → index. Cancellable."""
    global _last_hash
    log.info("rewind watcher starting", interval_s=interval, db=str(REWIND_DB))
    conn = _connect()
    _prune(conn)   # clear any backlog from before retention existed, on startup
    _ticks = 0
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
            active_app, window_title = await _active_context()
            ts = time.time()
            conn.execute(
                "INSERT INTO frames "
                "(ts, image_path, content_hash, ocr_text, active_app, window_title, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (ts, str(png_path), h, ocr_text, active_app, window_title, ts),
            )
            conn.commit()
            log.info(
                "rewind frame indexed",
                hash=h, ocr_chars=len(ocr_text), app=active_app or None,
            )
            _ticks += 1
            if _ticks % 12 == 0:   # ~hourly at the 5-min default
                _prune(conn)
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
