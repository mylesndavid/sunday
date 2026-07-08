"""Timeline — a semantic activity layer over the raw rewind frames.

Runs on the satellite, alongside `rewind_macos`. Rewind is the *capture* layer:
it drops a screenshot + OCR + active-app/window into `~/.sunday/rewind.db`.
Timeline is the *product* layer, built on Dayflow's two-stage model:

1. **Transcribe** (`transcribe_pending`): hand ~15 screenshots from each
   15-minute window to the LOCAL vision CLI (`chat_cli` → codex/claude) and get
   back timestamped **observations** — the play-by-play atoms.
2. **Synthesize** (`synthesize_recent`): hand the recent observations to the CLI
   and let the model group them into **cards** (title / summary /
   detailedSummary / category / distractions / appSites), merging by GOAL across
   app switches. A rolling window makes this idempotent for the mutable tail.

`process_pending` runs both stages under a wall-clock budget; the daemon calls it
and loops until the backlog drains. **Screenshots never leave the Mac** — they go
only to the user's own local CLI subscription. Cards + observations then roll up
into **Wrapped** (week / month / year), narrated daemon-side from derived stats.

Storage shares rewind's DB file so evidence (frames) and meaning (cards) live
together and the image IPC bridge keeps working: `timeline_observations`,
`timeline_events` (cards), `timeline_summaries` (Wrapped), and `timeline_state`
(pipeline cursors).
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
import time
from collections import Counter
from pathlib import Path
from typing import Any

import structlog

from sunday.devices import chat_cli, rewind_macos, timeline_video
from sunday.paths import sunday_home

log = structlog.get_logger("sunday.devices.timeline")

# Per-card timelapse clips live here — under the rewind dir (so the app's image
# IPC can read them) but as .mp4, which the frame pruner ignores (it only counts
# and deletes *.jpg). So they persist after the source frames are pruned.
EVIDENCE_DIR = rewind_macos.REWIND_DIR / "evidence"

# How many screenshots we hand the vision CLI per event. Dayflow samples ~15;
# we keep it lean since a session is usually one coherent thing.
SUMMARY_FRAME_SAMPLES = 12

# Shared with rewind — same file, same frames table.
TIMELINE_DB = rewind_macos.REWIND_DB

# ─── segmentation tunables ───────────────────────────────────────────────
# A gap longer than this between consecutive frames ends a session (you walked
# away, or switched to something unrelated long enough to count as new).
SEGMENT_GAP_SECONDS = 20 * 60
# Don't finalize the trailing segment until the last frame is at least this old
# — otherwise a still-growing "current" session gets chopped off early and
# re-segmented on the next pass. Same value as the gap: once a gap's worth of
# silence has passed, the session is definitely closed.
TRAILING_HOLD_SECONDS = SEGMENT_GAP_SECONDS
# Longest OCR/evidence blob we keep per event for later model summarization.
EVIDENCE_CHARS = 2400

# Map an app name to a coarse activity type. Matched case-insensitively as a
# substring so "Google Chrome", "Chrome Canary", "Cursor" all land right.
_APP_TYPES: list[tuple[tuple[str, ...], str]] = [
    (("cursor", "code", "xcode", "terminal", "iterm", "warp", "ghostty",
      "pycharm", "intellij", "zed", "nova", "sublime", "vim", "emacs"), "coding"),
    (("slack", "messages", "discord", "telegram", "whatsapp", "signal"), "messaging"),
    (("mail", "spark", "airmail", "outlook"), "email"),
    (("zoom", "facetime", "teams", "webex", "meet"), "meeting"),
    (("figma", "sketch", "photoshop", "illustrator", "affinity"), "design"),
    (("notion", "obsidian", "bear", "notes", "craft", "logseq", "word", "pages"), "writing"),
    (("chrome", "safari", "arc", "firefox", "brave", "edge", "orion"), "browsing"),
    (("spotify", "music", "quicktime", "tv", "vlc", "podcasts"), "media"),
    (("system settings", "system preferences", "finder", "calendar", "activity monitor"), "admin"),
]

_TYPE_LABEL = {
    "coding": "Coding", "messaging": "Messaging", "email": "Email",
    "meeting": "Meeting", "design": "Design", "writing": "Writing",
    "browsing": "Research", "media": "Media", "admin": "Admin", "other": "Activity",
}

# Rough per-type weight for importance scoring: deep-work types outrank ambient.
_TYPE_WEIGHT = {
    "coding": 1.0, "meeting": 0.95, "design": 0.9, "writing": 0.85,
    "email": 0.6, "browsing": 0.55, "messaging": 0.45, "admin": 0.4,
    "media": 0.2, "other": 0.5,
}


# ─── storage ─────────────────────────────────────────────────────────────


def _connect() -> sqlite3.Connection:
    TIMELINE_DB.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(TIMELINE_DB, check_same_thread=False)
    # Make sure the frames table + its richer columns exist even if the rewind
    # watcher hasn't run yet this boot (e.g. the UI opens Timeline first).
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
        CREATE TABLE IF NOT EXISTS timeline_events (
            id             INTEGER PRIMARY KEY AUTOINCREMENT,
            start_ts       REAL NOT NULL,
            end_ts         REAL NOT NULL,
            type           TEXT,
            title          TEXT NOT NULL,
            summary        TEXT,
            apps_json      TEXT,
            urls_json      TEXT,
            people_json    TEXT,
            projects_json  TEXT,
            frame_ids_json TEXT,
            scenes_json    TEXT,
            evidence_text  TEXT,
            dominant_app   TEXT,
            thumb_path     TEXT,
            confidence     REAL DEFAULT 0,
            importance     REAL DEFAULT 0,
            summarized     INTEGER DEFAULT 0,
            created_at     REAL NOT NULL,
            updated_at     REAL NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_tl_events_start ON timeline_events(start_ts);
        CREATE INDEX IF NOT EXISTS idx_tl_events_end   ON timeline_events(end_ts);
        CREATE INDEX IF NOT EXISTS idx_tl_events_type  ON timeline_events(type);
        CREATE TABLE IF NOT EXISTS timeline_summaries (
            id                INTEGER PRIMARY KEY AUTOINCREMENT,
            period_type       TEXT NOT NULL,
            period_start      REAL NOT NULL,
            period_end        REAL NOT NULL,
            title             TEXT,
            summary           TEXT,
            highlights_json   TEXT,
            projects_json     TEXT,
            people_json       TEXT,
            apps_json         TEXT,
            websites_json     TEXT,
            stats_json        TEXT,
            observations_json TEXT,
            generated_at      REAL NOT NULL
        );
        CREATE UNIQUE INDEX IF NOT EXISTS idx_tl_summaries_period
            ON timeline_summaries(period_type, period_start, period_end);
        CREATE TABLE IF NOT EXISTS timeline_observations (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            start_ts     REAL NOT NULL,
            end_ts       REAL NOT NULL,
            observation  TEXT NOT NULL,
            batch_start  REAL NOT NULL,
            created_at   REAL NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_tl_obs_start ON timeline_observations(start_ts);
        CREATE TABLE IF NOT EXISTS timeline_state (
            key   TEXT PRIMARY KEY,
            value TEXT
        );
        CREATE TABLE IF NOT EXISTS timeline_blocks (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            start_ts      REAL NOT NULL,
            end_ts        REAL NOT NULL,
            label         TEXT NOT NULL,
            intent        TEXT,               -- optional: what/why, for the drift check
            gcal_mode     TEXT DEFAULT 'none',-- none | busy | event
            gcal_event_id TEXT,               -- set when synced to Google Calendar
            status        TEXT DEFAULT 'planned', -- planned | done | skipped
            created_at    REAL NOT NULL,
            updated_at    REAL NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_tl_blocks_start ON timeline_blocks(start_ts);
        CREATE INDEX IF NOT EXISTS idx_tl_blocks_end   ON timeline_blocks(end_ts);
        """
    )
    rewind_macos._ensure_frame_columns(conn)
    _ensure_card_columns(conn)
    conn.commit()
    return conn


# Dayflow-shaped card fields the synthesis stage fills in, added to the base
# timeline_events table on any DB that predates them.
_CARD_EXTRA_COLUMNS = {
    "category":         "TEXT",
    "subcategory":      "TEXT",
    "detailed_summary": "TEXT",
    "distractions_json": "TEXT",
    "app_primary":      "TEXT",
    "app_secondary":    "TEXT",
    # Path to the card's baked H.264 timelapse (permanent visual evidence that
    # survives frame pruning). '' = not baked yet; 'none' = baked attempted but the
    # source frames were already gone (don't retry).
    "evidence_path":    "TEXT",
}


def _ensure_card_columns(conn: sqlite3.Connection) -> None:
    have = {r[1] for r in conn.execute("PRAGMA table_info(timeline_events)")}
    for col, decl in _CARD_EXTRA_COLUMNS.items():
        if col not in have:
            conn.execute(f"ALTER TABLE timeline_events ADD COLUMN {col} {decl}")


def _state_get(conn: sqlite3.Connection, key: str, default: float = 0.0) -> float:
    row = conn.execute("SELECT value FROM timeline_state WHERE key = ?", (key,)).fetchone()
    if not row:
        return default
    try:
        return float(row[0])
    except (TypeError, ValueError):
        return default


def _state_set(conn: sqlite3.Connection, key: str, value: Any) -> None:
    conn.execute(
        "INSERT INTO timeline_state (key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, str(value)),
    )


def _state_str(conn: sqlite3.Connection, key: str, default: str = "") -> str:
    row = conn.execute("SELECT value FROM timeline_state WHERE key = ?", (key,)).fetchone()
    return row[0] if row and row[0] is not None else default


def _record_result(ok: bool, error: str | None = None) -> None:
    """Persist the outcome of the last summarizer call so the UI can show whether
    processing is actually working, and surface the real error when it isn't
    (bad Gemini key, quota, CLI not logged in, …)."""
    conn = _connect()
    try:
        if ok:
            _state_set(conn, "last_ok_at", time.time())
            _state_set(conn, "last_error", "")
        else:
            _state_set(conn, "last_error", (error or "unknown error")[:400])
            _state_set(conn, "last_error_at", time.time())
        conn.commit()
    finally:
        conn.close()


# ─── classification + titling heuristics ─────────────────────────────────


def _app_type(app: str) -> str:
    a = (app or "").lower()
    for needles, kind in _APP_TYPES:
        if any(n in a for n in needles):
            return kind
    return "other"


def _clean_window_title(title: str, app: str) -> str:
    """Window titles carry the real subject (a file, a repo, a doc, a chat).
    Strip the app-name suffix apps love to append so "rewind_macos.py — sunday —
    Cursor" reads as "rewind_macos.py — sunday"."""
    t = (title or "").strip()
    if not t:
        return ""
    for sep in (" — ", " – ", " - ", " | "):
        parts = [p.strip() for p in t.split(sep) if p.strip()]
        if len(parts) > 1 and parts[-1].lower() == (app or "").lower():
            t = sep.join(parts[:-1])
    return t.strip()


def _heuristic_title(kind: str, dominant_app: str, window_titles: list[str]) -> str:
    """A decent title with no model in the loop — the model upgrades it later.
    Prefer the most common informative window title; fall back to type + app."""
    label = _TYPE_LABEL.get(kind, "Activity")
    cleaned = [_clean_window_title(w, dominant_app) for w in window_titles]
    cleaned = [c for c in cleaned if len(c) >= 3]
    if cleaned:
        subject = Counter(cleaned).most_common(1)[0][0]
        return f"{label} — {subject}"[:120]
    if dominant_app:
        return f"{label} in {dominant_app}"
    return label


def _importance(kind: str, duration_s: float) -> float:
    # Longer sessions of deep-work types matter more. Cap the duration term at
    # two hours so a single afternoon-long block doesn't peg everything at 1.0.
    dur_term = min(duration_s / (120 * 60), 1.0)
    return round(min(_TYPE_WEIGHT.get(kind, 0.5) * (0.4 + 0.6 * dur_term), 1.0), 3)


def _dedupe_ocr(texts: list[str], cap: int = EVIDENCE_CHARS) -> str:
    """Frames in a session repeat a lot of the same on-screen text. Keep unique
    lines in first-seen order until we hit the cap — this is the evidence blob
    the model summarizes from."""
    seen: set[str] = set()
    out: list[str] = []
    total = 0
    for txt in texts:
        for line in (txt or "").splitlines():
            s = line.strip()
            if len(s) < 4 or s in seen:
                continue
            seen.add(s)
            out.append(s)
            total += len(s) + 1
            if total >= cap:
                return "\n".join(out)
    return "\n".join(out)


# ─── two-stage pipeline: frames → observations → cards (Dayflow's model) ──
#
# We do NOT rule-segment. Stage 1 (transcribe) hands ~15 screenshots from each
# 15-minute window to the local vision CLI and gets back timestamped
# "observations" — the play-by-play atoms. Stage 2 (synthesize) hands the recent
# observations to the CLI and lets the model group them into cards, merging by
# GOAL across app switches, with title/summary/detailedSummary/category/
# distractions/appSites. Both prompts are Dayflow's, adapted to reference frame/
# observation INDICES instead of parsing clock strings (robust round-tripping).

BATCH_SECONDS        = 15 * 60   # transcription window length
BATCH_SAMPLES        = 15        # frames sampled per window (evenly)
BATCH_SETTLE_SECONDS = 120       # don't transcribe a window until it's this old
SYNTH_WINDOW_SECONDS = 60 * 60   # rolling window of observations re-grouped into cards


def _parse_json(raw: str):
    """Pull the first JSON object or array out of a model reply (tolerates code
    fences and surrounding prose)."""
    s = (raw or "").strip()
    if s.startswith("```"):
        s = s[s.find("\n") + 1: s.rfind("```")].strip()
    # Fast path: the reply IS clean JSON. Try the whole string first — otherwise a
    # wrapper object like {"segments":[...]} gets mis-parsed as its INNER array (the
    # bracket hunt below tries "[" first), which silently dropped every transcribe
    # observation (parsed came back a list, so .get("segments") never fired).
    try:
        return json.loads(s)
    except json.JSONDecodeError:
        pass
    for op, cl in (("[", "]"), ("{", "}")):
        a, b = s.find(op), s.rfind(cl)
        if a >= 0 and b > a:
            try:
                return json.loads(s[a:b + 1])
            except json.JSONDecodeError:
                continue
    return None


def _sample_frames(rows: list[tuple], n: int) -> list[dict]:
    """Evenly sample up to n (id, ts, image_path) rows whose image still exists."""
    if not rows:
        return []
    picks = rows if len(rows) <= n else \
        [rows[round(i * (len(rows) - 1) / (n - 1))] for i in range(n)]
    out = []
    for r in picks:
        if r[2] and Path(r[2]).exists():
            out.append({"id": r[0], "ts": r[1], "path": r[2],
                        "clock": time.strftime("%-I:%M %p", time.localtime(r[1]))})
    return out


_TRANSCRIBE_PROMPT = (
    "Analyze these {n} screenshots from a screen recording ({start} to {end}), in "
    "chronological order. Create an activity log detailed enough that someone "
    "could reconstruct EXACTLY what the user did.\n\n"
    "For each segment ask: \"What EXACTLY did they do? What SPECIFIC things can I "
    "see?\" Read app names from the macOS menu bar (top-left). Capture exact app/"
    "site names, file names, URLs, page titles, usernames, search queries, "
    "messages, numbers, view counts, prices.\n\n"
    "Bad: \"Checked email\"  Good: \"Gmail: read 'RE: Budget approval' from "
    "boss@co.com, replied 'looks good'\".\n"
    "Bad: \"Browsing YouTube\"  Good: \"YouTube: searched 'charlie brown "
    "christmas', watched Vince Guaraldi Trio (5.8M views)\".\n\n"
    "3-8 segments total. Use 1 only if idle for most of the recording. Group by "
    "GOAL not app (debugging across IDE+Terminal+Browser = 1 segment). Cover the "
    "whole range, no gaps.\n\n"
    "Screenshots are numbered 0..{last} at times: {clocks}.\n"
    "Return ONLY JSON: {{\"segments\":[{{\"startIndex\":0,\"endIndex\":3,"
    "\"description\":\"...\"}}]}} — indices reference the numbered screenshots."
)


async def resolve_summarizer() -> str | None:
    """Which backend turns screenshots into text: the user's explicit choice from
    Settings (`TIMELINE_MODEL` = codex|claude|gemini) when it's actually usable,
    else auto — a local CLI (codex/claude) if logged in, then Gemini if a key is
    set. None when nothing is available. This is the single knob every stage of
    the pipeline (and `state`) routes through."""
    from sunday.credentials import get_credential
    choice = (get_credential("TIMELINE_MODEL") or "auto").strip().lower()
    if choice == "gemini":
        return "gemini" if get_credential("GEMINI_API_KEY") else None
    if choice in ("codex", "claude"):
        return await chat_cli.detect(prefer=choice)
    # auto: prefer a local CLI (free + private), fall back to a Gemini key.
    tool = await chat_cli.detect()
    if tool:
        return tool
    return "gemini" if get_credential("GEMINI_API_KEY") else None


async def _summarize_run(prompt: str, image_paths: list[str] | None = None,
                         backend: str | None = None, timeout: float = 120.0,
                         record: bool = True) -> dict[str, Any]:
    """Dispatch one generation to the resolved backend. codex/claude → local CLI
    (screenshots stay on the Mac); gemini → cloud (cheap, images leave the Mac).
    Uniform {ok, text, error} return. Records the outcome so the UI can show
    working/failing (skip with record=False for throwaway probes)."""
    backend = backend or await resolve_summarizer()
    if backend == "gemini":
        from sunday.credentials import get_credential
        from sunday.devices import gemini_vision
        res = await gemini_vision.run(
            prompt, image_paths=image_paths, api_key=get_credential("GEMINI_API_KEY"),
            model=get_credential("GEMINI_MODEL"), timeout=timeout,
        )
    elif backend in ("codex", "claude"):
        res = await chat_cli.run(
            prompt, image_paths=image_paths, tool=backend,
            workdir=str(sunday_home()), timeout=timeout,
        )
    else:
        res = {"ok": False, "text": "", "error": "no timeline summarizer configured"}
    if record:
        _record_result(bool(res.get("ok")), res.get("error"))
    return res


# ─── background processor: drain the backlog without the UI open ──────────

_processor_task: asyncio.Task | None = None
PROCESSOR_INTERVAL = 25.0    # seconds between passes when there's a backlog


async def processor_loop() -> None:
    """Continuously turn captured frames into cards in the background, so the
    timeline builds whether or not the app is open. Idle-cheap: does nothing when
    there's no backlog or no summarizer configured. Each pass is wall-clock
    bounded by process_pending, so it never monopolizes the CLI/API."""
    log.info("timeline processor loop starting")
    while True:
        try:
            await asyncio.sleep(PROCESSOR_INTERVAL)
            if _pending_frames_count() <= 0:
                continue
            if not await resolve_summarizer():
                continue
            await process_pending(time_budget_s=90.0)
        except asyncio.CancelledError:
            log.info("timeline processor loop cancelled")
            return
        except Exception as exc:  # noqa: BLE001 — one bad pass must not kill the loop
            log.warning("timeline processor iteration failed", error=str(exc))
            _record_result(False, f"processor: {exc}")


def start_processor() -> dict[str, Any]:
    """Idempotently start the background processor (called on satellite boot)."""
    global _processor_task
    if _processor_task is not None and not _processor_task.done():
        return {"ok": True, "already_running": True}
    _processor_task = asyncio.create_task(processor_loop())
    return {"ok": True, "started": True}


async def test_summarizer(backend: str | None = None) -> dict[str, Any]:
    """Fire one tiny generation at the configured (or given) backend to prove the
    key/CLI actually works — the answer to "is my Gemini key even valid?". Records
    the result, so a passing test flips the UI to 'working' and a failing one
    surfaces the real error."""
    backend = backend or await resolve_summarizer()
    if not backend:
        return {"ok": False, "backend": None,
                "error": "No summarizer configured — pick Codex/Claude and log in, or Gemini + a key."}
    t0 = time.time()
    # Gemini answers in ~1-2s; a cold codex/claude CLI can take much longer, so
    # give the local backends real headroom rather than false-failing.
    timeout = 20.0 if backend == "gemini" else 55.0
    res = await _summarize_run("Reply with exactly: OK", backend=backend, timeout=timeout)
    return {
        "ok": bool(res.get("ok")),
        "backend": backend,
        "error": res.get("error"),
        "ms": round((time.time() - t0) * 1000),
        "reply": (res.get("text") or "").strip()[:60],
    }


async def transcribe_pending(tool: str | None = None, model: str | None = None,
                             time_budget_s: float = 90.0) -> dict[str, Any]:
    """Stage 1: turn each settled 15-minute frame window into observations via the
    configured backend (local CLI or Gemini). A cursor transcribes each once."""
    detected = await resolve_summarizer()
    conn = _connect()
    try:
        if not detected:
            return {"available": False, "transcribed": 0}
        cursor = _state_get(conn, "transcribed_through_ts", 0.0)
        win_start = cursor or conn.execute("SELECT MIN(ts) FROM frames").fetchone()[0]
        if win_start is None:
            return {"available": True, "transcribed": 0}
        started = time.time()
        made = 0
        workdir = str(sunday_home())
        while win_start + BATCH_SECONDS <= time.time() - BATCH_SETTLE_SECONDS:
            if time.time() - started > time_budget_s:
                break
            win_end = win_start + BATCH_SECONDS
            rows = conn.execute(
                "SELECT id, ts, image_path FROM frames WHERE ts >= ? AND ts < ? ORDER BY ts ASC",
                (win_start, win_end),
            ).fetchall()
            frames = _sample_frames(rows, BATCH_SAMPLES)
            if len(frames) >= 2:
                clocks = ", ".join(f"{i}:{f['clock']}" for i, f in enumerate(frames))
                prompt = _TRANSCRIBE_PROMPT.format(
                    n=len(frames), last=len(frames) - 1,
                    start=frames[0]["clock"], end=frames[-1]["clock"], clocks=clocks,
                )
                budget_left = max(20.0, time_budget_s - (time.time() - started))
                res = await _summarize_run(
                    prompt, image_paths=[f["path"] for f in frames], backend=detected,
                    timeout=min(chat_cli.TIMEOUT_SECONDS, budget_left),
                )
                if not res.get("ok"):
                    log.warning("timeline transcribe failed", error=res.get("error"))
                    break   # retry this window on the next call
                parsed = _parse_json(res.get("text") or "")
                # Accept both the documented {"segments":[...]} and a bare [...]
                # array — some backends drop the wrapper, and losing those would
                # again strand the window's frames with zero observations.
                if isinstance(parsed, dict):
                    segs = parsed.get("segments")
                elif isinstance(parsed, list):
                    segs = parsed
                else:
                    segs = None
                for seg in (segs or []):
                    try:
                        si = max(0, min(len(frames) - 1, int(seg.get("startIndex", 0))))
                        ei = max(si, min(len(frames) - 1, int(seg.get("endIndex", si))))
                    except (TypeError, ValueError):
                        continue
                    desc = (seg.get("description") or "").strip()
                    if desc:
                        conn.execute(
                            "INSERT INTO timeline_observations "
                            "(start_ts, end_ts, observation, batch_start, created_at) "
                            "VALUES (?,?,?,?,?)",
                            (frames[si]["ts"], frames[ei]["ts"], desc, win_start, time.time()),
                        )
                        made += 1
            _state_set(conn, "transcribed_through_ts", win_end)
            conn.commit()
            win_start = win_end
        return {"available": True, "transcribed": made}
    finally:
        conn.close()


_SYNTH_PROMPT = (
    "You are synthesizing a user's activity observations into timeline cards. "
    "Each card = one coherent activity. Time is a constraint, not a goal.\n\n"
    "MINIMUM 10 MINUTES PER CARD. If an activity would be shorter than 10 minutes, "
    "fold it into the neighboring card that makes the most sense — do NOT emit it "
    "as its own card. (The single exception: the very last card of the range may "
    "be shorter, because it's still in progress.)\n\n"
    "MERGE aggressively — default to merging. Switching apps/tools within one task "
    "is the SAME card (Figma→Meet→Figma for one review; IDE+Terminal+Browser "
    "debugging = one session). Two cards about the same work stream should almost "
    "never exist — if the previous card's main activity is the same PR / feature / "
    "article / codebase, merge. A brief (<5 min) unrelated detour (checking X, a "
    "message, a quick video) is a 'distraction' INSIDE the card, not a new card. "
    "Start a new card ONLY when the GOAL genuinely changes for 10+ minutes. If "
    "you're unsure whether to merge or split, MERGE.\n\n"
    "For each card:\n"
    "- title: specific; no 'and' joining unrelated things\n"
    "- summary: one sentence — what + why it mattered\n"
    "- detailedSummary: 2-4 sentences of concrete specifics\n"
    "- category: one of coding|browsing|writing|design|meeting|email|messaging|media|admin|other\n"
    "- distractions: [{{\"title\":\"\",\"summary\":\"\"}}] brief interruptions, or []\n"
    "- appSites: {{\"primary\":\"canonical domain e.g. figma.com, github.com, "
    "youtube.com, docs.google.com\",\"secondary\":\"\"}} — lower-case host, no "
    "protocol; omit secondary if none\n\n"
    "Observations are numbered 0..{last} at times: {clocks}.\n"
    "Return ONLY a JSON array of cards: [{{\"startIndex\":0,\"endIndex\":4,"
    "\"title\":\"\",\"summary\":\"\",\"detailedSummary\":\"\",\"category\":\"\","
    "\"distractions\":[],\"appSites\":{{\"primary\":\"\",\"secondary\":\"\"}}}}] — "
    "indices reference the numbered observations. Cover every observation in order, "
    "no gaps or overlaps.\n\n"
    "REMINDER: every card except the last must be at least 10 minutes long. Merge "
    "short activities into longer, meaningful cards that tell a coherent story — "
    "when in doubt, merge.\n\nObservations:\n{obs}"
)


# The synthesis prompt asks for a 10-min-minimum per card, but models don't always
# comply — a stray 3-min card renders as an unreadable sliver. Enforce it in code.
MIN_CARD_SECONDS = 10 * 60


def _merge_short_cards(cards: list, obs: list, exempt_last: bool = True) -> list:
    """Deterministically enforce the 10-minute floor: fold any card shorter than
    MIN_CARD_SECONDS into an adjacent card (previous by default, so it reads as a
    continuation), repeating until none remain. `exempt_last` leaves the final card
    alone — right for the live tail (still in progress), wrong for a settled
    historical window (every card there should meet the floor). Returns an ordered
    list of [start_index, end_index, data] referencing `obs`."""
    norm: list = []
    for c in cards:
        try:
            si = max(0, min(len(obs) - 1, int(c.get("startIndex", 0))))
            ei = max(si, min(len(obs) - 1, int(c.get("endIndex", si))))
        except (TypeError, ValueError):
            continue
        norm.append([si, ei, c])
    if not norm:
        return []
    norm.sort(key=lambda x: x[0])

    def _dur(item: list) -> float:
        return obs[item[1]]["end_ts"] - obs[item[0]]["start_ts"]

    changed = True
    while changed and len(norm) > 1:
        changed = False
        for idx in range(len(norm)):
            if exempt_last and idx == len(norm) - 1:
                continue                   # keep the in-progress tail card as-is
            if _dur(norm[idx]) >= MIN_CARD_SECONDS:
                continue
            if idx > 0:                    # fold into the previous card
                norm[idx - 1][1] = max(norm[idx - 1][1], norm[idx][1])
            else:                          # first card: fold into the next
                norm[idx + 1][0] = min(norm[idx + 1][0], norm[idx][0])
            del norm[idx]
            changed = True
            break
    return norm


def _set_state_val(key: str, value: Any) -> None:
    conn = _connect()
    try:
        _state_set(conn, key, value)
        conn.commit()
    finally:
        conn.close()


async def _synthesize_one_window(win_start: float, win_end: float, backend: str,
                                 freeze: bool, timeout: float) -> int:
    """Group the observations in [win_start, win_end) into cards. `freeze` means
    this is a settled historical window: advance the cursor past it and don't
    exempt its last card. The model call happens without a DB handle held open."""
    conn = _connect()
    try:
        rows = conn.execute(
            "SELECT start_ts, end_ts, observation FROM timeline_observations "
            "WHERE end_ts > ? AND start_ts < ? ORDER BY start_ts ASC",
            (win_start, win_end),
        ).fetchall()
    finally:
        conn.close()
    if not rows:
        if freeze:
            _set_state_val("cards_frozen_through_ts", win_end)   # skip an empty settled gap
        return 0

    obs = [{"start_ts": r[0], "end_ts": r[1], "text": r[2]} for r in rows]
    clocks = ", ".join(f"{i}:{time.strftime('%-I:%M %p', time.localtime(o['start_ts']))}"
                       for i, o in enumerate(obs))
    obs_txt = "\n".join(
        f"{i}. [{time.strftime('%-I:%M %p', time.localtime(o['start_ts']))}] {o['text']}"
        for i, o in enumerate(obs)
    )
    prompt = _SYNTH_PROMPT.format(last=len(obs) - 1, clocks=clocks, obs=obs_txt)
    res = await _summarize_run(prompt, backend=backend, timeout=timeout)
    if not res.get("ok"):
        # Transient (timeout / API). Do NOT freeze — retry this window next pass.
        log.warning("timeline synthesize failed", error=res.get("error"), win_start=int(win_start))
        return 0
    cards = _parse_json(res.get("text") or "")
    merged = _merge_short_cards(cards, obs, exempt_last=not freeze) if isinstance(cards, list) else []

    conn = _connect()
    try:
        # Only clear cards that START inside this window — never disturb prior windows'.
        conn.execute("DELETE FROM timeline_events WHERE start_ts >= ? AND start_ts < ?",
                     (win_start, win_end))
        made = 0
        now = time.time()
        for si, ei, c in merged:
            start_ts, end_ts = obs[si]["start_ts"], obs[ei]["end_ts"]
            cat = (c.get("category") or "other").strip().lower()
            if cat not in _TYPE_LABEL:
                cat = "other"
            appsites = c.get("appSites") if isinstance(c.get("appSites"), dict) else {}
            primary = (appsites.get("primary") or "").strip().lower()
            secondary = (appsites.get("secondary") or "").strip().lower()
            distractions = c.get("distractions") if isinstance(c.get("distractions"), list) else []
            title = (c.get("title") or _TYPE_LABEL.get(cat, "Activity")).strip()[:160]
            conn.execute(
                "INSERT INTO timeline_events "
                "(start_ts, end_ts, type, category, subcategory, title, summary, "
                " detailed_summary, distractions_json, app_primary, app_secondary, "
                " apps_json, urls_json, people_json, projects_json, frame_ids_json, "
                " dominant_app, importance, summarized, created_at, updated_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (start_ts, end_ts, cat, cat, (c.get("subcategory") or "").strip()[:80],
                 title, (c.get("summary") or "").strip()[:600],
                 (c.get("detailedSummary") or "").strip()[:1600],
                 json.dumps(distractions), primary, secondary,
                 json.dumps([primary] if primary else []), json.dumps([]),
                 json.dumps([]), json.dumps([]), json.dumps([]),
                 primary or "", _importance(cat, end_ts - start_ts), 1, now, now),
            )
            made += 1
        # Freeze on model SUCCESS even if it yielded no cards, so a content-empty
        # window can't block history drainage forever (only transient failures retry).
        if freeze:
            _state_set(conn, "cards_frozen_through_ts", win_end)
        conn.commit()
    finally:
        conn.close()
    if made:
        log.info("timeline synthesized window", cards=made, obs=len(obs),
                 win_start=int(win_start), frozen=freeze)
    return made


async def synthesize_recent(tool: str | None = None, model: str | None = None,
                            timeout: float = 120.0) -> dict[str, Any]:
    """Stage 2: group observations into cards, draining ALL un-synthesized history
    window by window — not just the last hour (the bug that left big backlogs at a
    handful of cards). Settled windows are frozen as they complete so the cursor
    rolls forward; the live tail stays re-groupable so a session spanning several
    transcription batches still lands as one merged card."""
    detected = await resolve_summarizer()
    if not detected:
        return {"available": False, "cards": 0}
    started = time.time()
    made_total = 0
    windows = 0
    MAX_WINDOWS = 40   # backstop: bound work per call, the loop drains over calls
    while time.time() - started < timeout and windows < MAX_WINDOWS:
        conn = _connect()
        try:
            cursor = _state_get(conn, "cards_frozen_through_ts", 0.0)
            first = conn.execute(
                "SELECT MIN(start_ts) FROM timeline_observations WHERE end_ts > ?", (cursor,)
            ).fetchone()[0]
        finally:
            conn.close()
        if first is None:
            break                          # all observations accounted for
        now = time.time()
        win_start = max(cursor, first)
        if win_start + SYNTH_WINDOW_SECONDS <= now - BATCH_SETTLE_SECONDS:
            win_end = win_start + SYNTH_WINDOW_SECONDS   # settled historical window
            freeze = True
        else:
            win_end = now                                 # the live, still-growing tail
            freeze = False
        remaining = timeout - (time.time() - started)
        made = await _synthesize_one_window(
            win_start, win_end, detected, freeze,
            timeout=max(20.0, min(chat_cli.TIMEOUT_SECONDS, remaining)),
        )
        made_total += made
        windows += 1
        if not freeze:
            break                          # reached the live tail; nothing settled left
        # If a settled window didn't advance the cursor, it hit a transient failure
        # — stop this pass rather than hammering the same window; retry next call.
        conn = _connect()
        try:
            moved = _state_get(conn, "cards_frozen_through_ts", 0.0) > cursor
        finally:
            conn.close()
        if not moved:
            break
    return {"available": True, "cards": made_total, "windows": windows}


def _pending_frames_count() -> int:
    """Frames captured but not yet transcribed into observations."""
    conn = _connect()
    try:
        cursor = _state_get(conn, "transcribed_through_ts", 0.0)
        settled = time.time() - BATCH_SETTLE_SECONDS
        return conn.execute(
            "SELECT COUNT(*) FROM frames WHERE ts > ? AND ts < ?", (cursor, settled)
        ).fetchone()[0]
    finally:
        conn.close()


async def process_pending(tool: str | None = None, model: str | None = None,
                          time_budget_s: float = 110.0) -> dict[str, Any]:
    """Run both stages under a wall-clock budget: transcribe settled frame windows,
    then re-synthesize recent observations into cards. Returns coverage so the
    daemon/UI can poll until the backlog drains."""
    t = await transcribe_pending(tool=tool, model=model, time_budget_s=time_budget_s * 0.7)
    if not t.get("available"):
        return {"available": False, "transcribed": 0, "cards": 0,
                "remaining": _pending_frames_count()}
    s = await synthesize_recent(tool=tool, model=model, timeout=max(30.0, time_budget_s * 0.4))
    # Bake per-card timelapses while the source frames still exist. Cheap and
    # bounded; runs after synthesis so new cards get their permanent clip promptly.
    baked = await bake_pending_evidence(time_budget_s=min(30.0, time_budget_s * 0.25))
    # Refresh the drift verdict for the active block (rate-limited internally, and a
    # no-op when no block is running). Non-fatal.
    try:
        await block_drift_check()
    except Exception as exc:  # noqa: BLE001
        log.warning("block drift check failed", error=str(exc))
    return {"available": True, "transcribed": t.get("transcribed", 0),
            "cards": s.get("cards", 0), "baked": baked.get("baked", 0),
            "remaining": _pending_frames_count()}


async def bake_pending_evidence(time_budget_s: float = 30.0, max_cards: int = 20) -> dict[str, Any]:
    """Encode a timelapse MP4 for cards that don't have one yet, newest-first (most
    likely to still have their frames). Stores it on the card so the evidence
    survives frame pruning. Idempotent and bounded: skips cards already baked or
    marked 'none' (frames gone), stops at the time budget. Best-effort — a failed
    encode just leaves the card unbaked for a later pass."""
    if timeline_video.ffmpeg_exe() is None:
        return {"baked": 0, "no_ffmpeg": True}   # nothing to do; retry when it's available
    conn = _connect()
    try:
        rows = conn.execute(
            "SELECT id, start_ts, end_ts FROM timeline_events "
            "WHERE (evidence_path IS NULL OR evidence_path = '') "
            "ORDER BY start_ts DESC LIMIT ?", (max_cards,),
        ).fetchall()
    finally:
        conn.close()
    if not rows:
        return {"baked": 0}
    started = time.time()
    baked = 0
    for eid, start_ts, end_ts in rows:
        if time.time() - started > time_budget_s:
            break
        conn = _connect()
        try:
            frows = conn.execute(
                "SELECT ts, image_path FROM frames WHERE ts >= ? AND ts <= ? ORDER BY ts ASC",
                (start_ts, end_ts),
            ).fetchall()
        finally:
            conn.close()
        frames = [{"ts": r[0], "image_path": r[1]} for r in frows]
        paths = timeline_video.sample_frames(frames)
        if not paths:
            # Frames already pruned — mark so we don't retry this card forever.
            _set_evidence(eid, "none")
            continue
        out = str(EVIDENCE_DIR / f"card_{eid}.mp4")
        ok = await timeline_video.encode_timelapse(paths, out)
        if ok:
            _set_evidence(eid, out)
            baked += 1
        # If encode failed transiently, leave evidence_path='' to retry next pass.
    return {"baked": baked}


def _set_evidence(event_id: int, path: str) -> None:
    conn = _connect()
    try:
        conn.execute("UPDATE timeline_events SET evidence_path = ? WHERE id = ?", (path, int(event_id)))
        conn.commit()
    finally:
        conn.close()


def observations(from_ts: float, to_ts: float) -> dict[str, Any]:
    """The play-by-play atoms overlapping a time range — used to show a card's
    minute-by-minute breakdown in the detail pane."""
    conn = _connect()
    try:
        rows = conn.execute(
            "SELECT start_ts, end_ts, observation FROM timeline_observations "
            "WHERE end_ts >= ? AND start_ts <= ? ORDER BY start_ts ASC",
            (from_ts, to_ts),
        ).fetchall()
        return {"observations": [
            {"start_ts": r[0], "end_ts": r[1], "text": r[2],
             "time": time.strftime("%-I:%M %p", time.localtime(r[0]))}
            for r in rows
        ]}
    finally:
        conn.close()


# ─── serialization ───────────────────────────────────────────────────────


def _event_dict(row: sqlite3.Row | tuple, cols: list[str]) -> dict[str, Any]:
    d = dict(zip(cols, row))
    for jkey in ("apps_json", "urls_json", "people_json", "projects_json", "distractions_json"):
        raw = d.pop(jkey, None)
        try:
            d[jkey[:-5]] = json.loads(raw) if raw else []
        except (TypeError, json.JSONDecodeError):
            d[jkey[:-5]] = []
    # Only expose a real clip path; '' (not baked yet) and 'none' (frames were
    # already gone) both mean "no clip" to the UI.
    ev = (d.get("evidence_path") or "").strip()
    d["evidence_path"] = ev if ev and ev != "none" else None
    return d


_EVENT_COLS = [
    "id", "start_ts", "end_ts", "type", "category", "subcategory", "title",
    "summary", "detailed_summary", "distractions_json", "app_primary",
    "app_secondary", "apps_json", "urls_json", "people_json", "projects_json",
    "dominant_app", "importance", "summarized", "evidence_path",
]
_EVENT_SELECT = (
    "SELECT id, start_ts, end_ts, type, category, subcategory, title, summary, "
    "detailed_summary, distractions_json, app_primary, app_secondary, apps_json, "
    "urls_json, people_json, projects_json, dominant_app, importance, summarized, "
    "evidence_path "
    "FROM timeline_events"
)


# ─── queries ─────────────────────────────────────────────────────────────


def events(
    from_ts: float | None = None,
    to_ts: float | None = None,
    limit: int = 500,
    type: str | None = None,
    project: str | None = None,
    q: str | None = None,
) -> list[dict[str, Any]]:
    """Cards overlapping [from_ts, to_ts], newest first, with optional type/
    project/text filters. Cards are produced by the transcribe→synthesize
    pipeline (process_pending); reads never block on the model."""
    conn = _connect()
    try:
        where = ["end_ts >= ?" if from_ts else "1"]
        args: list[Any] = [from_ts] if from_ts else []
        if to_ts:
            where.append("start_ts <= ?")
            args.append(to_ts)
        if type:
            where.append("type = ?")
            args.append(type)
        if project:
            where.append("(projects_json LIKE ? OR app_primary LIKE ?)")
            args += [f"%{project}%", f"%{project}%"]
        if q:
            where.append("(title LIKE ? OR summary LIKE ? OR detailed_summary LIKE ?)")
            args += [f"%{q}%", f"%{q}%", f"%{q}%"]
        sql = f"{_EVENT_SELECT} WHERE {' AND '.join(where)} ORDER BY start_ts DESC LIMIT ?"
        args.append(int(limit))
        rows = conn.execute(sql, args).fetchall()
        return [_event_dict(r, _EVENT_COLS) for r in rows]
    finally:
        conn.close()


def _local_day_bounds(date_str: str) -> tuple[float, float]:
    t = time.strptime(date_str, "%Y-%m-%d")
    start = time.mktime((t.tm_year, t.tm_mon, t.tm_mday, 0, 0, 0, 0, 0, -1))
    return start, start + 86400


def day(date: str) -> dict[str, Any]:
    """All events for a local calendar day (YYYY-MM-DD), oldest first — the
    natural reading order for 'what did I do today'."""
    start, end = _local_day_bounds(date)
    evs = events(from_ts=start, to_ts=end, limit=1000)
    evs.sort(key=lambda e: e["start_ts"])
    return {"date": date, "events": evs}


def search(q: str, from_ts: float | None = None, to_ts: float | None = None,
           limit: int = 50) -> list[dict[str, Any]]:
    if not (q or "").strip():
        return []
    return events(from_ts=from_ts, to_ts=to_ts, limit=limit, q=q.strip())


# ─── timeblocks: intentions (vs cards, which are reality) ──────────────────
# A block is a commitment to YOURSELF — "2–4: Gravity deep work". Private/local by
# default (gcal_mode='none'); optionally mirrored to Google Calendar as busy or a
# full event. The menu bar shows the current block as a live contract, and later
# the drift check compares a block's intent against the observed cards/observations.

_BLOCK_COLS = ["id", "start_ts", "end_ts", "label", "intent", "gcal_mode",
               "gcal_event_id", "status", "created_at", "updated_at"]
_BLOCK_MODES = ("none", "busy", "event")


def _block_row(conn: sqlite3.Connection, block_id: int) -> dict[str, Any] | None:
    row = conn.execute(
        f"SELECT {', '.join(_BLOCK_COLS)} FROM timeline_blocks WHERE id = ?", (int(block_id),)
    ).fetchone()
    return dict(zip(_BLOCK_COLS, row)) if row else None


def block_set(start_ts: float, end_ts: float, label: str, intent: str | None = None,
              gcal_mode: str = "none", block_id: int | None = None) -> dict[str, Any]:
    """Create or (if block_id given) update a timeblock. Returns {"block": {...}}."""
    label = (label or "").strip()
    if not label:
        return {"error": "'label' is required"}
    try:
        start_ts, end_ts = float(start_ts), float(end_ts)
    except (TypeError, ValueError):
        return {"error": "start_ts/end_ts must be unix seconds"}
    if end_ts <= start_ts:
        return {"error": "end_ts must be after start_ts"}
    gcal_mode = gcal_mode if gcal_mode in _BLOCK_MODES else "none"
    intent = (intent or "").strip() or None
    now = time.time()
    conn = _connect()
    try:
        if block_id:
            if not _block_row(conn, block_id):
                return {"error": f"no block #{block_id}"}
            conn.execute(
                "UPDATE timeline_blocks SET start_ts=?, end_ts=?, label=?, intent=?, "
                "gcal_mode=?, updated_at=? WHERE id=?",
                (start_ts, end_ts, label, intent, gcal_mode, now, int(block_id)),
            )
            conn.commit()
            return {"block": _block_row(conn, block_id)}
        cur = conn.execute(
            "INSERT INTO timeline_blocks (start_ts, end_ts, label, intent, gcal_mode, "
            "status, created_at, updated_at) VALUES (?,?,?,?,?, 'planned', ?, ?)",
            (start_ts, end_ts, label, intent, gcal_mode, now, now),
        )
        conn.commit()
        return {"block": _block_row(conn, cur.lastrowid)}
    finally:
        conn.close()


def blocks(from_ts: float, to_ts: float) -> dict[str, Any]:
    """All blocks overlapping [from_ts, to_ts), oldest first."""
    conn = _connect()
    try:
        rows = conn.execute(
            f"SELECT {', '.join(_BLOCK_COLS)} FROM timeline_blocks "
            "WHERE end_ts > ? AND start_ts < ? ORDER BY start_ts ASC",
            (float(from_ts), float(to_ts)),
        ).fetchall()
        return {"blocks": [dict(zip(_BLOCK_COLS, r)) for r in rows]}
    finally:
        conn.close()


def block_clear(block_id: int) -> dict[str, Any]:
    """Delete a block (and its calendar mirror is the caller's concern)."""
    conn = _connect()
    try:
        conn.execute("DELETE FROM timeline_blocks WHERE id = ?", (int(block_id),))
        conn.commit()
        return {"ok": True, "id": int(block_id)}
    finally:
        conn.close()


def current_block() -> dict[str, Any]:
    """The block covering now (if any) + the next upcoming block — the menu bar's
    live contract. Includes seconds remaining / until for the countdown."""
    now = time.time()
    conn = _connect()
    try:
        cur = conn.execute(
            f"SELECT {', '.join(_BLOCK_COLS)} FROM timeline_blocks "
            "WHERE start_ts <= ? AND end_ts > ? ORDER BY start_ts DESC LIMIT 1",
            (now, now),
        ).fetchone()
        nxt = conn.execute(
            f"SELECT {', '.join(_BLOCK_COLS)} FROM timeline_blocks "
            "WHERE start_ts > ? ORDER BY start_ts ASC LIMIT 1", (now,),
        ).fetchone()
        current = dict(zip(_BLOCK_COLS, cur)) if cur else None
        nextb = dict(zip(_BLOCK_COLS, nxt)) if nxt else None
        # Attach the cached drift verdict for the current block, if it's fresh.
        if current:
            raw = _state_str(conn, f"drift_{current['id']}", "")
            if raw:
                try:
                    d = json.loads(raw)
                    if now - float(d.get("at") or 0) <= DRIFT_WINDOW_SECONDS:
                        current["drift"] = {"on_track": d.get("on_track"), "note": d.get("note") or ""}
                except (json.JSONDecodeError, TypeError, ValueError):
                    pass
        return {
            "now": now,
            "current": current,
            "next": nextb,
            "ends_in_s": (current["end_ts"] - now) if current else None,
            "next_in_s": (nextb["start_ts"] - now) if nextb else None,
        }
    finally:
        conn.close()


DRIFT_WINDOW_SECONDS = 15 * 60


async def block_drift_check() -> dict[str, Any]:
    """The killer feature: judge whether the user's ACTUAL recent activity matches
    the CURRENT block's intention — reusing the observations we already collect, so
    no other tool can do this. Cheap text-only LLM call, only when a block is
    active; result cached in timeline_state so the menu bar reads it without an LLM
    per poll. Not a nag — just an honest 'you said Gravity, you're in Slack chaos'."""
    cb = current_block()
    cur = cb.get("current")
    if not cur:
        return {"active": False}
    now = time.time()
    # Rate-limit the LLM: serve a cached verdict if it's under ~3 min old.
    conn = _connect()
    try:
        raw = _state_str(conn, f"drift_{cur['id']}", "")
    finally:
        conn.close()
    if raw:
        try:
            d = json.loads(raw)
            if now - float(d.get("at") or 0) < 180:
                return {"active": True, "on_track": d.get("on_track"),
                        "note": d.get("note") or "", "cached": True}
        except (json.JSONDecodeError, TypeError, ValueError):
            pass
    lo = max(float(cur["start_ts"]), now - DRIFT_WINDOW_SECONDS)
    obs = observations(lo, now).get("observations", [])
    if not obs:
        return {"active": True, "on_track": None, "note": "nothing observed yet"}
    detected = await resolve_summarizer()
    if not detected:
        return {"active": True, "on_track": None, "note": "no summarizer"}
    recent = "; ".join(o["text"] for o in obs[-8:])[:1200]
    intent = cur["label"] + (f" — {cur['intent']}" if cur.get("intent") else "")
    prompt = (
        "The user set this timeblock (their intention for right now):\n"
        f'  "{intent}"\n\n'
        "In the last few minutes they actually did:\n"
        f"  {recent}\n\n"
        "Is their actual activity consistent with the timeblock's intention? Be "
        "lenient about tools (ANY app that serves the goal counts — IDE, browser, "
        "docs, terminal), but honest about clear drift (social media, unrelated "
        "browsing, a different project). Return ONLY JSON: "
        '{"on_track": true|false, "note": "<= 8 words, e.g. \'deep in the repo\' or '
        "'scrolling X, not Gravity'\"}."
    )
    res = await _summarize_run(prompt, backend=detected, timeout=30)
    if not res.get("ok"):
        return {"active": True, "on_track": None, "note": "check failed"}
    parsed = _parse_json(res.get("text") or "")
    on_track = bool(parsed.get("on_track")) if isinstance(parsed, dict) else None
    note = ((parsed.get("note") if isinstance(parsed, dict) else "") or "")[:60]
    _set_state_val(f"drift_{cur['id']}", json.dumps(
        {"on_track": on_track, "note": note, "at": now, "block_id": cur["id"]}))
    return {"active": True, "on_track": on_track, "note": note}


def event_frames(event_id: int) -> dict[str, Any]:
    """The raw frames within a card's time span — evidence for the detail-pane
    scrubber. Cards no longer store frame ids (they come from observations), so
    we fetch by time range."""
    conn = _connect()
    try:
        row = conn.execute(
            "SELECT start_ts, end_ts FROM timeline_events WHERE id = ?", (int(event_id),)
        ).fetchone()
        if not row:
            return {"frames": []}
        frows = conn.execute(
            "SELECT id, ts, image_path, ocr_text, active_app, window_title "
            "FROM frames WHERE ts >= ? AND ts <= ? ORDER BY ts ASC",
            (row[0], row[1]),
        ).fetchall()
        return {"frames": [
            {"id": r[0], "ts": r[1], "image_path": r[2],
             "ocr_text": (r[3] or "")[:1500], "active_app": r[4], "window_title": r[5]}
            for r in frows
        ]}
    finally:
        conn.close()


async def state() -> dict[str, Any]:
    """Capture state (shared with rewind) plus timeline coverage. Includes `cli`
    — which local summarizer (codex/claude) is available, or None — so the UI can
    tell "still building" apart from "no summarizer logged in"."""
    from sunday.credentials import get_credential
    base = rewind_macos.stats()
    try:
        summarizer = await resolve_summarizer()
    except Exception:  # noqa: BLE001
        summarizer = None
    conn = _connect()
    try:
        cards = conn.execute("SELECT COUNT(*) FROM timeline_events").fetchone()[0]
        obs = conn.execute("SELECT COUNT(*) FROM timeline_observations").fetchone()[0]
        base.update({
            "events": cards,
            "cards": cards,
            "observations": obs,
            "pending_frames": _pending_frames_count(),
            "transcribed_through": _state_get(conn, "transcribed_through_ts", 0.0),
            # Active summarizer backend + whether it runs on-device. `cli` kept as
            # an alias so any older reader still sees "is a summarizer available".
            "summarizer": summarizer,
            "summarizer_local": summarizer in ("codex", "claude"),
            "cli": summarizer,
            # Raw config for the Settings selector (the choice, not the resolved).
            "summarizer_choice": (get_credential("TIMELINE_MODEL") or "auto").strip().lower(),
            "gemini_key_set": bool(get_credential("GEMINI_API_KEY")),
            # Observability: is processing actually working, and if not, why.
            "last_ok_at": _state_get(conn, "last_ok_at", 0.0) or None,
            "last_error_at": _state_get(conn, "last_error_at", 0.0) or None,
            "last_error": _state_str(conn, "last_error") or None,
        })
        return base
    finally:
        conn.close()


# ─── capture toggle (delegates to rewind) ────────────────────────────────


def storage_usage() -> dict[str, Any]:
    """How much disk Sunday's data uses, broken down. Measures ~/.sunday on THIS
    machine (the satellite that owns the data). frames = capturable screenshots
    (hard-capped by rewind_macos), clips = permanent per-card timelapses (currently
    UNCAPPED), db = the sqlite files, other = everything else under ~/.sunday."""
    home = sunday_home()

    def _sum(paths) -> int:
        t = 0
        for p in paths:
            try:
                if p.is_file():
                    t += p.stat().st_size
            except OSError:
                pass
        return t

    total = _sum(home.rglob("*")) if home.exists() else 0
    frames = _sum(rewind_macos.REWIND_DIR.rglob("*.jpg")) if rewind_macos.REWIND_DIR.exists() else 0
    clips = _sum(EVIDENCE_DIR.rglob("*.mp4")) if EVIDENCE_DIR.exists() else 0
    chrome_dir = home / "chrome"
    browser = _sum(chrome_dir.rglob("*")) if chrome_dir.exists() else 0
    dbs = 0
    if home.exists():
        dbs = _sum(home.glob("*.db")) + _sum(home.glob("*.db-*"))
    other = max(0, total - frames - clips - browser - dbs)
    return {
        "total_bytes": total,
        "frames_bytes": frames,
        "clips_bytes": clips,
        "browser_bytes": browser,                       # per-task Chrome profiles — cleanable cruft
        "db_bytes": dbs,
        "other_bytes": other,
        "frames_cap_mb": rewind_macos.MAX_TOTAL_MB,     # frames are hard-capped at this
        "frames_retention_days": rewind_macos.RETENTION_DAYS,
        "clips_capped": False,                          # honest: clips currently grow unbounded
    }


def start(interval: float | None = None) -> dict[str, Any]:
    kwargs = {"interval": interval} if interval else {}
    return rewind_macos.start(**kwargs)


def stop() -> dict[str, Any]:
    return rewind_macos.stop()


def reprocess() -> dict[str, Any]:
    """Re-derive cards from what we STILL HAVE — never a destructive from-frames
    wipe. Cards are the permanent record; frames are pruned after RETENTION_DAYS,
    so deleting cards to "rebuild from frames" can destroy history whose source
    frames are already gone (it did, once). So:

    - HARD GUARD: if there are no observations to rebuild from, do nothing. Cards
      stay put. (This is the check that would have prevented wiping the timeline.)
    - Otherwise only re-synthesize the span COVERED BY EXISTING OBSERVATIONS:
      clear cards from the first surviving observation onward and rewind the
      synthesis cursor so they re-group under the current prompts. Older cards,
      whose frames + observations are long pruned, are left untouched.
    - Never resets the transcription cursor — we don't try to re-read frames that
      may no longer exist."""
    conn = _connect()
    try:
        obs = conn.execute("SELECT COUNT(*) FROM timeline_observations").fetchone()[0]
        cards = conn.execute("SELECT COUNT(*) FROM timeline_events").fetchone()[0]
        if obs == 0:
            # Nothing to rebuild FROM. Refuse to touch the cards — they may be the
            # only surviving record (frames pruned). Fail loud, change nothing.
            log.warning("timeline reprocess refused: no observations to rebuild from",
                        cards=cards)
            return {"ok": False, "skipped": True,
                    "reason": "No observations to rebuild from — cards left untouched "
                              "to avoid destroying history whose frames are already pruned.",
                    "cards": cards}
        first_obs = conn.execute(
            "SELECT MIN(start_ts) FROM timeline_observations").fetchone()[0]
        # Re-synthesize only the observation-covered span; preserve older cards.
        conn.execute("DELETE FROM timeline_events WHERE end_ts >= ?", (first_obs,))
        _state_set(conn, "cards_frozen_through_ts", 0.0)
        _state_set(conn, "last_error", "")
        _state_set(conn, "last_error_at", 0.0)
        conn.commit()
        log.info("timeline reprocess: re-synthesizing from existing observations",
                 observations=obs, cleared_cards_from=first_obs, kept_older_cards=True)
        return {"ok": True, "observations": obs, "resynthesize_from": first_obs}
    finally:
        conn.close()


# ─── Wrapped: aggregate on the Mac, narrate on the daemon ─────────────────


def _capped_duration(start_ts: float, end_ts: float) -> float:
    """An event's contribution to 'active time'. Cap a single event at 2h so a
    session left open over lunch doesn't inflate the totals."""
    return min(end_ts - start_ts, 2 * 3600)


def period_stats(period_start: float, period_end: float) -> dict[str, Any]:
    """Everything Wrapped needs, computed locally: the structured stats plus the
    event titles/summaries the daemon turns into a narrative. Cheap — pure SQL +
    Python over one period's events."""
    conn = _connect()
    try:
        rows = conn.execute(
            _EVENT_SELECT + " WHERE start_ts >= ? AND start_ts < ? ORDER BY start_ts ASC",
            (period_start, period_end),
        ).fetchall()
        evs = [_event_dict(r, _EVENT_COLS) for r in rows]
        if not evs:
            return {"empty": True, "events": []}

        app_minutes: Counter = Counter()
        type_minutes: Counter = Counter()
        project_counts: Counter = Counter()
        people_counts: Counter = Counter()
        day_minutes: Counter = Counter()
        total_min = 0.0
        for e in evs:
            mins = _capped_duration(e["start_ts"], e["end_ts"]) / 60
            total_min += mins
            if e.get("dominant_app"):
                app_minutes[e["dominant_app"]] += mins
            type_minutes[e.get("type") or "other"] += mins
            for p in e.get("projects", []):
                project_counts[p] += 1
            for person in e.get("people", []):
                people_counts[person] += 1
            day = time.strftime("%Y-%m-%d", time.localtime(e["start_ts"]))
            day_minutes[day] += mins

        longest = sorted(
            evs, key=lambda e: e["end_ts"] - e["start_ts"], reverse=True
        )[:5]
        busiest_day = day_minutes.most_common(1)[0][0] if day_minutes else None

        # Compact event list for the narrator — title + type + when, no OCR.
        digest = [
            {"title": e["title"], "type": e.get("type"),
             "min": round(_capped_duration(e["start_ts"], e["end_ts"]) / 60),
             "day": time.strftime("%a %b %d", time.localtime(e["start_ts"])),
             "projects": e.get("projects", [])}
            for e in evs
        ]
        return {
            "empty": False,
            "period_start": period_start,
            "period_end": period_end,
            "event_count": len(evs),
            "active_minutes": round(total_min),
            "top_apps": [{"app": a, "minutes": round(m)} for a, m in app_minutes.most_common(8)],
            "by_type": [{"type": t, "minutes": round(m)} for t, m in type_minutes.most_common()],
            "top_projects": [{"project": p, "events": c} for p, c in project_counts.most_common(8)],
            "top_people": [{"person": p, "events": c} for p, c in people_counts.most_common(8)],
            "day_breakdown": [{"day": d, "minutes": round(m)} for d, m in sorted(day_minutes.items())],
            "busiest_day": busiest_day,
            "longest_sessions": [
                {"title": e["title"], "minutes": round((e["end_ts"] - e["start_ts"]) / 60),
                 "day": time.strftime("%a %b %d", time.localtime(e["start_ts"]))}
                for e in longest
            ],
            "events_digest": digest,
        }
    finally:
        conn.close()


def apply_wrapped(
    period_type: str, period_start: float, period_end: float,
    title: str | None = None, summary: str | None = None,
    highlights: list | None = None, projects: list | None = None,
    people: list | None = None, apps: list | None = None,
    websites: list | None = None, stats: dict | None = None,
    observations: list | None = None,
) -> dict[str, Any]:
    """Store a generated Wrapped for a period (upsert on the unique period key)."""
    conn = _connect()
    try:
        conn.execute(
            "INSERT INTO timeline_summaries "
            "(period_type, period_start, period_end, title, summary, highlights_json, "
            " projects_json, people_json, apps_json, websites_json, stats_json, "
            " observations_json, generated_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(period_type, period_start, period_end) DO UPDATE SET "
            "title=excluded.title, summary=excluded.summary, highlights_json=excluded.highlights_json, "
            "projects_json=excluded.projects_json, people_json=excluded.people_json, "
            "apps_json=excluded.apps_json, websites_json=excluded.websites_json, "
            "stats_json=excluded.stats_json, observations_json=excluded.observations_json, "
            "generated_at=excluded.generated_at",
            (period_type, period_start, period_end, title, summary,
             json.dumps(highlights or []), json.dumps(projects or []),
             json.dumps(people or []), json.dumps(apps or []),
             json.dumps(websites or []), json.dumps(stats or {}),
             json.dumps(observations or []), time.time()),
        )
        conn.commit()
        return {"ok": True}
    finally:
        conn.close()


def get_wrapped(period_type: str, period_start: float, period_end: float) -> dict[str, Any]:
    """Fetch a previously generated Wrapped, or {"found": False}."""
    conn = _connect()
    try:
        row = conn.execute(
            "SELECT title, summary, highlights_json, projects_json, people_json, "
            "apps_json, websites_json, stats_json, observations_json, generated_at "
            "FROM timeline_summaries WHERE period_type = ? AND period_start = ? AND period_end = ?",
            (period_type, period_start, period_end),
        ).fetchone()
        if not row:
            return {"found": False}
        def _j(x: str | None) -> Any:
            try:
                return json.loads(x) if x else []
            except json.JSONDecodeError:
                return []
        return {
            "found": True, "period_type": period_type,
            "period_start": period_start, "period_end": period_end,
            "title": row[0], "summary": row[1],
            "highlights": _j(row[2]), "projects": _j(row[3]), "people": _j(row[4]),
            "apps": _j(row[5]), "websites": _j(row[6]), "stats": _j(row[7]) or {},
            "observations": _j(row[8]), "generated_at": row[9],
        }
    finally:
        conn.close()
