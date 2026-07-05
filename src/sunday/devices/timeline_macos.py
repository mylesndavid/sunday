"""Timeline — a semantic activity layer over the raw rewind frames.

Runs on the satellite, alongside `rewind_macos`. Rewind is the *capture*
layer: it drops a screenshot + OCR + active-app/window every few minutes into
`~/.sunday/rewind.db`. Timeline is the *product* layer: it groups those frames
into human-readable **events** ("9:10–10:25 · Coding — sunday"), and rolls
events up into **Wrapped** summaries (week / month / year).

Division of labour, deliberately:

* **Segmentation is local + rule-based** and lives here. Frames → events using
  time gaps and (smoothed) active-app changes. No model, no network. Cheap
  enough to run incrementally every time the UI loads.
* **Summarization + Wrapped narratives need a model**, which the satellite
  doesn't have. So this module produces *derived text* (window titles, OCR
  excerpts, app lists) and exposes it via `unsummarized()` / `period_stats()`.
  The daemon reads that text, runs the model, and writes results back through
  `apply_summary()` / `apply_wrapped()`. **Screenshots never leave the Mac** —
  only derived text does, and only when the user has capture on.

Storage shares rewind's DB file so evidence (the frames) and meaning (the
events) live together and the existing image IPC bridge keeps working. We add
two tables — `timeline_events`, `timeline_summaries` — plus a tiny
`timeline_state` for the incremental-segmentation cursor.
"""

from __future__ import annotations

import json
import sqlite3
import time
from collections import Counter
from pathlib import Path
from typing import Any

import structlog

from sunday.devices import chat_cli, rewind_macos
from sunday.paths import sunday_home

log = structlog.get_logger("sunday.devices.timeline")

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
        CREATE TABLE IF NOT EXISTS timeline_state (
            key   TEXT PRIMARY KEY,
            value TEXT
        );
        """
    )
    rewind_macos._ensure_frame_columns(conn)
    conn.commit()
    return conn


def _state_get(conn: sqlite3.Connection, key: str, default: float = 0.0) -> float:
    row = conn.execute("SELECT value FROM timeline_state WHERE key = ?", (key,)).fetchone()
    if not row:
        return default
    try:
        return float(row[0])
    except (TypeError, ValueError):
        return default


def _state_set(conn: sqlite3.Connection, key: str, value: float) -> None:
    conn.execute(
        "INSERT INTO timeline_state (key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, str(value)),
    )


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


# ─── segmenter ───────────────────────────────────────────────────────────


def _smoothed_apps(frames: list[dict]) -> list[str]:
    """Kill single-frame app flickers: a frame's app only counts if it matches a
    neighbour. Otherwise inherit the previous frame's app. Prevents a two-second
    glance at Slack mid-coding from shattering the session into three events."""
    apps = [(f.get("active_app") or "") for f in frames]
    out: list[str] = []
    for i, app in enumerate(apps):
        prev = apps[i - 1] if i > 0 else ""
        nxt = apps[i + 1] if i + 1 < len(apps) else ""
        if app and (app == prev or app == nxt):
            out.append(app)
        elif out:
            out.append(out[-1])
        else:
            out.append(app)
    return out


def _build_event_row(frames: list[dict], smoothed: list[str]) -> tuple:
    """Turn one finalized run of frames into a timeline_events insert tuple."""
    start_ts = frames[0]["ts"]
    end_ts = frames[-1]["ts"]
    app_counts = Counter(a for a in smoothed if a)
    dominant_app = app_counts.most_common(1)[0][0] if app_counts else ""
    kind = _app_type(dominant_app)
    apps = [a for a, _ in app_counts.most_common()]
    window_titles = [f.get("window_title") or "" for f in frames]
    urls = sorted({(f.get("browser_url") or "").strip() for f in frames if f.get("browser_url")})
    frame_ids = [f["id"] for f in frames]
    evidence = _dedupe_ocr([f.get("ocr_text") or "" for f in frames])
    title = _heuristic_title(kind, dominant_app, window_titles)
    importance = _importance(kind, end_ts - start_ts)
    thumb = frames[len(frames) // 2]["image_path"]
    now = time.time()
    return (
        start_ts, end_ts, kind, title, None,
        json.dumps(apps), json.dumps(urls), json.dumps([]), json.dumps([]),
        json.dumps(frame_ids), json.dumps([]), evidence, dominant_app, thumb,
        0.5, importance, 0, now, now,
    )


def segment(lookback_hours: float = 72.0) -> dict[str, Any]:
    """Incrementally turn un-segmented frames into events. Only frames newer than
    the stored cursor are considered, and the still-open trailing session is held
    back until a gap proves it's closed — so already-built events (and their
    model summaries) are never rewritten. Idempotent and cheap; safe to call on
    every timeline load."""
    conn = _connect()
    try:
        cursor = _state_get(conn, "segmented_through_ts", 0.0)
        floor = max(cursor, time.time() - lookback_hours * 3600)
        rows = conn.execute(
            "SELECT id, ts, image_path, ocr_text, active_app, window_title, browser_url "
            "FROM frames WHERE ts > ? ORDER BY ts ASC",
            (floor,),
        ).fetchall()
        if not rows:
            return {"created": 0, "segmented_through": cursor}
        frames = [
            {"id": r[0], "ts": r[1], "image_path": r[2], "ocr_text": r[3],
             "active_app": r[4], "window_title": r[5], "browser_url": r[6]}
            for r in rows
        ]
        smoothed = _smoothed_apps(frames)

        # Walk frames, cutting a boundary on a long gap or a smoothed-app change.
        segments: list[tuple[int, int]] = []   # (start_idx, end_idx) inclusive
        seg_start = 0
        for i in range(1, len(frames)):
            gap = frames[i]["ts"] - frames[i - 1]["ts"]
            app_changed = bool(smoothed[i]) and smoothed[i] != smoothed[i - 1]
            if gap > SEGMENT_GAP_SECONDS or app_changed:
                segments.append((seg_start, i - 1))
                seg_start = i
        segments.append((seg_start, len(frames) - 1))

        # Hold back the trailing segment if it might still be growing (its last
        # frame is recent). Everything before it is safe to finalize.
        now = time.time()
        if frames[-1]["ts"] > now - TRAILING_HOLD_SECONDS and segments:
            segments = segments[:-1]
        if not segments:
            return {"created": 0, "segmented_through": cursor}

        created = 0
        last_finalized_ts = cursor
        for a, b in segments:
            group = frames[a:b + 1]
            row = _build_event_row(group, smoothed[a:b + 1])
            conn.execute(
                "INSERT INTO timeline_events "
                "(start_ts, end_ts, type, title, summary, apps_json, urls_json, "
                " people_json, projects_json, frame_ids_json, scenes_json, evidence_text, "
                " dominant_app, thumb_path, confidence, importance, summarized, "
                " created_at, updated_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                row,
            )
            created += 1
            last_finalized_ts = max(last_finalized_ts, group[-1]["ts"])
        _state_set(conn, "segmented_through_ts", last_finalized_ts)
        conn.commit()
        log.info("timeline segmented", created=created, through=last_finalized_ts)
        return {"created": created, "segmented_through": last_finalized_ts}
    finally:
        conn.close()


# ─── serialization ───────────────────────────────────────────────────────


def _event_dict(row: sqlite3.Row | tuple, cols: list[str]) -> dict[str, Any]:
    d = dict(zip(cols, row))
    for jkey in ("apps_json", "urls_json", "people_json", "projects_json",
                 "frame_ids_json", "scenes_json"):
        raw = d.pop(jkey, None)
        try:
            d[jkey[:-5]] = json.loads(raw) if raw else []
        except (TypeError, json.JSONDecodeError):
            d[jkey[:-5]] = []
    d.pop("evidence_text", None)   # never ship the raw OCR blob to the UI
    return d


_EVENT_COLS = [
    "id", "start_ts", "end_ts", "type", "title", "summary", "apps_json",
    "urls_json", "people_json", "projects_json", "frame_ids_json", "scenes_json",
    "dominant_app", "thumb_path", "confidence", "importance", "summarized",
]
_EVENT_SELECT = (
    "SELECT id, start_ts, end_ts, type, title, summary, apps_json, urls_json, "
    "people_json, projects_json, frame_ids_json, scenes_json, dominant_app, "
    "thumb_path, confidence, importance, summarized FROM timeline_events"
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
    """Events overlapping [from_ts, to_ts], newest first. Optional type/project/
    text filters. Segmentation is the caller's job (daemon segments first)."""
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
            where.append("projects_json LIKE ?")
            args.append(f"%{project}%")
        if q:
            where.append("(title LIKE ? OR summary LIKE ? OR evidence_text LIKE ?)")
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


def event_frames(event_id: int) -> dict[str, Any]:
    """The frames behind one event — evidence for the detail-pane scrubber."""
    conn = _connect()
    try:
        row = conn.execute(
            "SELECT frame_ids_json FROM timeline_events WHERE id = ?", (int(event_id),)
        ).fetchone()
        if not row:
            return {"frames": []}
        try:
            ids = json.loads(row[0] or "[]")
        except json.JSONDecodeError:
            ids = []
        if not ids:
            return {"frames": []}
        qmarks = ",".join("?" * len(ids))
        frows = conn.execute(
            f"SELECT id, ts, image_path, ocr_text, active_app, window_title "
            f"FROM frames WHERE id IN ({qmarks}) ORDER BY ts ASC",
            ids,
        ).fetchall()
        return {"frames": [
            {"id": r[0], "ts": r[1], "image_path": r[2],
             "ocr_text": (r[3] or "")[:1500], "active_app": r[4], "window_title": r[5]}
            for r in frows
        ]}
    finally:
        conn.close()


def state() -> dict[str, Any]:
    """Capture state (shared with rewind) plus timeline coverage."""
    base = rewind_macos.stats()
    conn = _connect()
    try:
        total_events = conn.execute("SELECT COUNT(*) FROM timeline_events").fetchone()[0]
        unsummarized = conn.execute(
            "SELECT COUNT(*) FROM timeline_events WHERE summarized = 0"
        ).fetchone()[0]
        base.update({
            "events": total_events,
            "unsummarized": unsummarized,
            "segmented_through": _state_get(conn, "segmented_through_ts", 0.0),
        })
        return base
    finally:
        conn.close()


# ─── capture toggle (delegates to rewind) ────────────────────────────────


def start(interval: float | None = None) -> dict[str, Any]:
    kwargs = {"interval": interval} if interval else {}
    return rewind_macos.start(**kwargs)


def stop() -> dict[str, Any]:
    return rewind_macos.stop()


# ─── summarization plumbing (daemon runs the model, we hold the text) ────


def _frame_timeline(conn: sqlite3.Connection, frame_ids: list[int], cap: int = 40) -> list[dict]:
    """A compact, chronological per-frame trail for one event — timestamp + app
    + window + a short OCR snippet per captured moment. This is what lets the
    model write a timestamped play-by-play ('9:37–9:40: searched X, watched Y')
    instead of a single flat summary. Bounded to `cap` frames so a long session
    doesn't blow the prompt."""
    if not frame_ids:
        return []
    ids = frame_ids[:cap] if len(frame_ids) <= cap else \
        [frame_ids[round(i * (len(frame_ids) - 1) / (cap - 1))] for i in range(cap)]
    qmarks = ",".join("?" * len(ids))
    rows = conn.execute(
        f"SELECT ts, active_app, window_title, ocr_text FROM frames "
        f"WHERE id IN ({qmarks}) ORDER BY ts ASC",
        ids,
    ).fetchall()
    out = []
    for ts, app, win, ocr in rows:
        clock = time.strftime("%-I:%M %p", time.localtime(ts))
        snippet = " ".join((ocr or "").split())[:220]
        out.append({"clock": clock, "app": app or "", "window": win or "", "ocr": snippet})
    return out


def unsummarized(limit: int = 12) -> dict[str, Any]:
    """Events still on their heuristic title, with the derived text the model
    needs to write a real title, summary, and a timestamped play-by-play. No
    screenshots — OCR snippets + window titles + apps + timestamps only."""
    conn = _connect()
    try:
        rows = conn.execute(
            "SELECT id, start_ts, end_ts, type, dominant_app, apps_json, "
            "frame_ids_json, evidence_text FROM timeline_events "
            "WHERE summarized = 0 ORDER BY start_ts DESC LIMIT ?",
            (int(limit),),
        ).fetchall()
        out = []
        for r in rows:
            try:
                apps = json.loads(r[5] or "[]")
            except json.JSONDecodeError:
                apps = []
            try:
                fids = json.loads(r[6] or "[]")
            except json.JSONDecodeError:
                fids = []
            out.append({
                "id": r[0], "start_ts": r[1], "end_ts": r[2], "type": r[3],
                "dominant_app": r[4], "apps": apps,
                "duration_min": round((r[2] - r[1]) / 60, 1),
                "frames": _frame_timeline(conn, fids),
                "evidence": (r[7] or "")[:EVIDENCE_CHARS],
            })
        return {"events": out}
    finally:
        conn.close()


def apply_summary(
    id: int, title: str | None = None, summary: str | None = None,
    type: str | None = None, projects: list | None = None,
    people: list | None = None, importance: float | None = None,
    scenes: list | None = None,
) -> dict[str, Any]:
    """Write a model-produced summary back onto an event and mark it done.
    `scenes` is the minute-by-minute play-by-play shown when the card is opened:
    a list of {"time": "9:37–9:40", "text": "..."} objects."""
    conn = _connect()
    try:
        sets = ["summarized = 1", "updated_at = ?"]
        args: list[Any] = [time.time()]
        if title:
            sets.append("title = ?"); args.append(title[:160])
        if summary is not None:
            sets.append("summary = ?"); args.append(summary[:800])
        if type:
            sets.append("type = ?"); args.append(type)
        if projects is not None:
            sets.append("projects_json = ?"); args.append(json.dumps(projects))
        if people is not None:
            sets.append("people_json = ?"); args.append(json.dumps(people))
        if scenes is not None:
            sets.append("scenes_json = ?"); args.append(json.dumps(scenes))
        if importance is not None:
            sets.append("importance = ?"); args.append(float(importance))
        args.append(int(id))
        conn.execute(f"UPDATE timeline_events SET {', '.join(sets)} WHERE id = ?", args)
        conn.commit()
        return {"ok": True, "id": int(id)}
    finally:
        conn.close()


# ─── vision summarizer (local CLI reads the screenshots, never uploads) ──


_SUMMARY_SYSTEM = (
    "You reconstruct exactly what a person did during ONE screen session from "
    "its screenshots, in order. Read app names from the macOS menu bar; read the "
    "exact titles, URLs, search queries, view counts, file names, messages, and "
    "numbers you can see. Be specific and concrete like a narrator — never vague, "
    "never invented.\n\n"
    "Reply with ONLY a JSON object, no prose, no code fence:\n"
    "{\"title\": short specific title, <=8 words, no app name unless meaningful;\n"
    " \"summary\": 1-2 sentences on what they did and why it mattered;\n"
    " \"type\": one of coding|messaging|email|meeting|design|writing|browsing|media|admin|other;\n"
    " \"projects\": [repo/product/doc names you can identify];\n"
    " \"people\": [names of people involved];\n"
    " \"scenes\": [a minute-by-minute play-by-play, one object per distinct thing "
    "they did, in order: {\"time\": \"9:37–9:40 AM\", \"text\": \"searched "
    "'charlie brown christmas', watched Vince Guaraldi Trio (5.8M views)\"}]}\n"
    "Ground every field strictly in what is visible in the screenshots."
)


def _parse_json(raw: str) -> dict | None:
    s = (raw or "").strip()
    if s.startswith("```"):
        s = s[s.find("\n") + 1: s.rfind("```")].strip()
    a, b = s.find("{"), s.rfind("}")
    if a < 0 or b <= a:
        return None
    try:
        d = json.loads(s[a:b + 1])
        return d if isinstance(d, dict) else None
    except json.JSONDecodeError:
        return None


def _sample_event_frames(conn: sqlite3.Connection, frame_ids: list[int],
                         n: int = SUMMARY_FRAME_SAMPLES) -> list[dict]:
    """Evenly sample up to `n` frames of an event and return the ones whose image
    still exists on disk, with their clock times (so the model can timestamp the
    play-by-play)."""
    if not frame_ids:
        return []
    picks = frame_ids if len(frame_ids) <= n else \
        [frame_ids[round(i * (len(frame_ids) - 1) / (n - 1))] for i in range(n)]
    qmarks = ",".join("?" * len(picks))
    rows = conn.execute(
        f"SELECT ts, image_path FROM frames WHERE id IN ({qmarks}) ORDER BY ts ASC",
        picks,
    ).fetchall()
    out = []
    for ts, path in rows:
        if path and Path(path).exists():
            out.append({"clock": time.strftime("%-I:%M %p", time.localtime(ts)), "path": path})
    return out


async def summarize_pending(
    limit: int = 6, tool: str | None = None, model: str | None = None,
    time_budget_s: float = 110.0,
) -> dict[str, Any]:
    """Turn heuristic-titled events into real titles + play-by-play scenes by
    handing each event's screenshots to the local codex/claude CLI. Runs on the
    Mac; images never leave it. Processes events until `limit` or the wall-clock
    budget is hit, so the daemon can call it under a bounded WS timeout and loop.
    Returns {available, summarized, remaining, tool}."""
    detected = await chat_cli.detect(prefer=tool)
    conn = _connect()
    try:
        remaining = conn.execute(
            "SELECT COUNT(*) FROM timeline_events WHERE summarized = 0"
        ).fetchone()[0]
        if not detected:
            return {"available": False, "summarized": 0, "remaining": remaining, "tool": None}
        rows = conn.execute(
            "SELECT id, start_ts, end_ts, frame_ids_json FROM timeline_events "
            "WHERE summarized = 0 ORDER BY start_ts DESC LIMIT ?",
            (int(limit),),
        ).fetchall()
        started = time.time()
        done = 0
        workdir = str(sunday_home())
        for eid, start_ts, end_ts, fids_json in rows:
            if time.time() - started > time_budget_s:
                break
            try:
                fids = json.loads(fids_json or "[]")
            except json.JSONDecodeError:
                fids = []
            frames = _sample_event_frames(conn, fids)
            if not frames:
                # No evidence on disk (pruned) — mark done so we stop retrying.
                conn.execute(
                    "UPDATE timeline_events SET summarized = 1, updated_at = ? WHERE id = ?",
                    (time.time(), eid),
                )
                conn.commit()
                continue
            when = (f"{time.strftime('%A %-I:%M %p', time.localtime(start_ts))} to "
                    f"{time.strftime('%-I:%M %p', time.localtime(end_ts))}")
            clocks = ", ".join(f["clock"] for f in frames)
            prompt = (
                f"{_SUMMARY_SYSTEM}\n\nSession: {when}. {len(frames)} screenshots, "
                f"in order, at these times: {clocks}.\n\nJSON:"
            )
            budget_left = max(20.0, time_budget_s - (time.time() - started))
            res = await chat_cli.run(
                prompt, image_paths=[f["path"] for f in frames],
                tool=detected, model=model, workdir=workdir,
                timeout=min(chat_cli.TIMEOUT_SECONDS, budget_left),
            )
            if not res.get("ok"):
                log.warning("timeline vision summary failed", id=eid, error=res.get("error"))
                continue
            parsed = _parse_json(res.get("text") or "")
            if not parsed:
                continue
            scenes = parsed.get("scenes")
            apply_summary(
                id=eid,
                title=(parsed.get("title") or "").strip() or None,
                summary=(parsed.get("summary") or "").strip() or None,
                type=parsed.get("type"),
                projects=parsed.get("projects") if isinstance(parsed.get("projects"), list) else None,
                people=parsed.get("people") if isinstance(parsed.get("people"), list) else None,
                scenes=scenes if isinstance(scenes, list) else None,
            )
            done += 1
        remaining = conn.execute(
            "SELECT COUNT(*) FROM timeline_events WHERE summarized = 0"
        ).fetchone()[0]
        return {"available": True, "summarized": done, "remaining": remaining, "tool": detected}
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
