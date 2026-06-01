#!/usr/bin/env python3
"""Observer — ambient producer of "now" + atoms + conversations.

Two outputs, one mic loop:
  • Per tick (~30s): transcribe audio → LLM with v2 prompt + open atoms
    → POST `now` to daemon, POST new_atoms, POST atom_updates.
  • Per conversation close (4 consecutive silent chunks ≈ 2 min): fire a
    structured-summary LLM call over the buffered transcript → POST
    /v1/conversations; any atoms born during the window get linked back.

Launched by Sunday.app as a child process when the user flips the toggle.
Audio stays on this Mac (mic → Whisper → discarded); only structured text
(now/atoms/conversation summary) is sent to the daemon.

    python3 observer.py [daemon_url] [duration_sec] [chunk_sec]
"""
import json, os, subprocess, sys, time, urllib.request
from datetime import datetime

DAEMON = sys.argv[1] if len(sys.argv) > 1 else "https://sunday.betterbotagent.com"
DUR    = int(sys.argv[2]) if len(sys.argv) > 2 else 1800
CHUNK  = int(sys.argv[3]) if len(sys.argv) > 3 else 30
MODEL  = "deepseek/deepseek-v4-flash"
MIC    = "1"
LOG    = os.path.expanduser("~/.sunday/observer.log")
WAV_DIR = os.path.expanduser("~/.sunday")
os.makedirs(WAV_DIR, exist_ok=True)

# 2 min of silence closes a conversation (OMI's pattern, verbatim).
SILENT_CHUNKS_TO_CLOSE = max(2, 120 // CHUNK)


def _find(prog, *fallbacks):
    for p in fallbacks:
        if os.path.exists(p):
            return p
    return prog
FFMPEG = _find("ffmpeg", "/opt/homebrew/bin/ffmpeg", "/usr/local/bin/ffmpeg")
CURL   = _find("curl",   "/usr/bin/curl")


# ── prompts ────────────────────────────────────────────────────────────────

TICK_SYSTEM = """You are the observation engine. From recent speech (and, when available, recent activity), maintain TWO distinct stores:

  WORKING (live; decays; can be reinforced; eligible for nudges)
    Kinds: commitment, thread, deadline.

  REFERENCE (immutable; never decays; never in active count; surfaced on request)
    Kinds: decision, fact.

Atomicity (most important rule)
  Each atom is ONE action with ONE completion signal. Multiple actions → SEPARATE atoms. "Review the spec, figure out billing, build a dashboard" → THREE atoms. If you can't write one sentence describing what makes it done, split.

Discriminating commitment from thread (the fuzzy line)
  • commitment — single owner, single observable completion signal. "I'll send the deck Friday."
  • thread     — ongoing multi-party topic with no single done-state. "We've been going back and forth with Manuel about infra." Threads NEVER complete — they only decay (stale → dropped).
  • deadline   — like commitment but with an explicit date.

Ownership (required, never null)
  "you" for first person, a name when someone else committed, "unclear" ONLY when you genuinely can't tell. Unclear-owner working atoms are HELD: no nudges, faster decay, promoted to "you" or a name if a later observation clarifies.

Completion signal
  REQUIRED for commitment and deadline. A concrete observable the system could plausibly see ("Slack DM to Manuel", "PR opened", "calendar event created"). OMIT for thread and REFERENCE.

Text is immutable.
  Once an atom exists, its text doesn't change. Circumstances changed ("actually push to next week")? CREATE a new atom + emit supersede on the old (action=superseded, state→superseded, superseded_by → the new atom or "new:<index>"). Never edit.

Per tick, for each open WORKING atom decide one of:
  • REINFORCED  — current activity is consistent with active work on this atom. Resets decay. State unchanged.
  • CLOSED      — completion signal observed. state → completed. Evidence + confidence REQUIRED.
  • DROPPED     — user abandoned. state → dropped. Evidence + confidence REQUIRED.
  • SUPERSEDED  — replaced by a new atom. state → superseded, superseded_by → new id (or "new:<index>"). Evidence REQUIRED.
  • (no change) — no signal either way.

Confidence (required on closed/dropped/superseded; 0.0–1.0)
  ≥ 0.85: strong direct signal. 0.6–0.85: plausible inference. < 0.6: do NOT close.

You also produce the "now" line for the hub:
  • "now": one short present-tense line, 4–10 words. "Idle / no clear activity" if unclear. "Watching/listening to a video about X" if system audio rather than them.
  • "same_as_last": TRUE if continues prior `now`; FALSE on a real shift.

Be calm and sparing. Chit-chat, pleasantries, filler → no atoms. Silence/noise → empty arrays.

Return ONLY JSON:
{
  "now": "...",
  "same_as_last": true|false,
  "atom_updates": [{"id": <int>, "action": "reinforced"|"closed"|"dropped"|"superseded", "state": "...", "evidence": "...", "confidence": 0.0, "superseded_by": <id>|"new:<index>", "why": "..."}],
  "new_atoms": [{"text": "<one atomic action>", "kind": "commitment|thread|deadline|decision|fact", "owner": "you"|"<name>"|"unclear", "completion_signal": "<observable>"}],
  "nudge": null
}"""

CONV_SYSTEM = """Summarize a real conversation transcript captured from a mic (single channel; the user is the dominant voice, others may come through speakers, and some chunks may be video/podcast bleed).

Return ONLY JSON:
{
  "title": "3–7 words, no quotes",
  "summary": "one tight paragraph, 2–4 sentences — what was talked about and what (if anything) was decided",
  "category": "work" | "personal" | "meeting" | "call" | "media" | "unclear",
  "participants": ["the user" or names heard (e.g. 'Zach'), plus 'video/podcast' if it's clearly that]
}

If the transcript is mostly a YouTube/podcast/ad and not a real two-way conversation, set category="media" and title="Video/audio: <topic>". Be honest — empty-feeling chunks should reflect that, not be inflated."""


# ── credentials + helpers ──────────────────────────────────────────────────

def get_key(name):
    k = os.environ.get(name)
    if k:
        return k
    p = os.path.expanduser("~/.sunday/credentials.env")
    if os.path.exists(p):
        for line in open(p):
            if line.startswith(name + "="):
                v = line.split("=", 1)[1].strip().strip('"').strip("'")
                if v:
                    return v
    try:
        return subprocess.run(["security", "find-generic-password", "-s", name, "-w"],
                              capture_output=True, text=True).stdout.strip() or None
    except Exception:
        return None


OR_KEY  = get_key("OPENROUTER_API_KEY")
OAI_KEY = get_key("OPENAI_API_KEY")


def log(msg):
    line = f"{datetime.now().strftime('%H:%M:%S')}  {msg}"
    print(line, flush=True)
    try:
        with open(LOG, "a") as f:
            f.write(line + "\n")
    except Exception:
        pass


def record(secs):
    wav = os.path.join(WAV_DIR, "observer_chunk.wav")
    try:
        subprocess.run([FFMPEG, "-y", "-f", "avfoundation", "-i", f":{MIC}",
                        "-t", str(secs), "-ac", "1", "-ar", "16000", wav],
                       capture_output=True, timeout=secs + 15)
    except Exception:
        return None
    return wav if os.path.exists(wav) and os.path.getsize(wav) > 2000 else None


def transcribe(wav, purpose="observer_tick"):
    if not wav or not OAI_KEY:
        return ""
    try:
        t0 = time.time()
        r = subprocess.run([CURL, "-s", "https://api.openai.com/v1/audio/transcriptions",
                            "-H", f"Authorization: Bearer {OAI_KEY}",
                            "-F", f"file=@{wav}", "-F", "model=whisper-1"],
                           capture_output=True, text=True, timeout=60)
        latency_ms = int((time.time() - t0) * 1000)
        text = (json.loads(r.stdout).get("text") or "").strip()
        # Audio cost is per-minute of input regardless of transcript length;
        # report the WAV duration we asked for (CHUNK) to the cost meter.
        push_cost(kind="audio", purpose=purpose, provider="openai",
                  model="whisper-1", audio_seconds=float(CHUNK), latency_ms=latency_ms)
        return text
    except Exception:
        return ""


def llm(system, user, purpose="observer_tick"):
    body = json.dumps({"model": MODEL,
                       "messages": [{"role": "system", "content": system},
                                    {"role": "user",   "content": user}],
                       "provider": {"sort": "latency"},
                       # Ask OpenRouter for token usage in the response so cost
                       # is accurate (not a char-based estimate).
                       "usage": {"include": True}}).encode()
    req = urllib.request.Request("https://openrouter.ai/api/v1/chat/completions", data=body,
                                 headers={"Authorization": f"Bearer {OR_KEY}", "Content-Type": "application/json"})
    t0 = time.time()
    resp = json.load(urllib.request.urlopen(req, timeout=45))
    latency_ms = int((time.time() - t0) * 1000)
    txt = resp["choices"][0]["message"]["content"].strip()
    usage = resp.get("usage") or {}
    push_cost(kind="llm", purpose=purpose, provider="openrouter", model=MODEL,
              prompt_tokens=int(usage.get("prompt_tokens") or 0),
              completion_tokens=int(usage.get("completion_tokens") or 0),
              latency_ms=latency_ms)
    if txt.startswith("```"):
        txt = txt[txt.find("\n") + 1: txt.rfind("```")].strip()
    try:
        return json.loads(txt)
    except json.JSONDecodeError:
        a, b = txt.find("{"), txt.rfind("}")
        return json.loads(txt[a:b + 1]) if a >= 0 and b > a else {}


# ── daemon push helpers ────────────────────────────────────────────────────

def _post(path, payload):
    req = urllib.request.Request(f"{DAEMON}{path}", data=json.dumps(payload).encode(),
                                 headers={"Content-Type": "application/json"})
    return json.load(urllib.request.urlopen(req, timeout=20))


def _get(path):
    return json.load(urllib.request.urlopen(f"{DAEMON}{path}", timeout=15))


def push_cost(*, kind, purpose, provider, model, prompt_tokens=0,
              completion_tokens=0, audio_seconds=0.0, latency_ms=0):
    """Best-effort cost telemetry to the daemon. Never raises — observability
    that crashes the producer is worse than no observability."""
    try:
        _post("/v1/cost/log", {
            "kind": kind, "purpose": purpose, "provider": provider, "model": model,
            "prompt_tokens": prompt_tokens, "completion_tokens": completion_tokens,
            "audio_seconds": audio_seconds, "latency_ms": latency_ms,
        })
    except Exception:
        pass


def push_now(text, same):
    return _post("/v1/observer/now", {"now": text, "same_as_last": bool(same)})


def push_atom(na, evidence=None):
    return _post("/v1/atoms", {
        "text": na.get("text"),
        "kind": na.get("kind"),
        "owner": na.get("owner"),
        "completion_signal": na.get("completion_signal"),
        "evidence": evidence,
        "source": "observer",
    })


def push_atom_update(u):
    aid = int(u.get("id"))
    return _post(f"/v1/atoms/{aid}", {
        "action": u.get("action"),
        "state":  u.get("state"),
        "evidence": u.get("evidence"),
        "confidence": u.get("confidence"),
        "superseded_by": u.get("superseded_by"),
    })


def push_conversation(start_ts, ended_at, summary, transcript):
    return _post("/v1/conversations", {
        "started_at": start_ts,
        "ended_at": ended_at,
        "title": summary.get("title"),
        "summary": summary.get("summary"),
        "category": summary.get("category"),
        "participants": summary.get("participants") or [],
        "transcript": transcript,
        "link_atoms_since": start_ts,
    })


def fetch_open_atoms(limit=20):
    try:
        d = _get(f"/v1/atoms?state=active&limit={limit}")
        return d.get("atoms") or []
    except Exception:
        return []


# ── main loop ──────────────────────────────────────────────────────────────

def close_conversation(start_ts, buffer):
    if not buffer:
        return
    transcript = "\n".join(t for _, t in buffer)
    end_ts = time.time()
    try:
        s = llm(CONV_SYSTEM, f"Transcript:\n\n{transcript}\n\nSummary JSON:", purpose="observer_conv_close")
    except Exception as e:
        log(f"  (conv summary err: {e})")
        s = {"title": "Untitled conversation", "summary": "", "category": "unclear"}
    try:
        r = push_conversation(start_ts, end_ts, s, transcript)
        log(f"📜 conversation closed id={r.get('id')} linked_atoms={r.get('linked_atoms')} "
            f"title={s.get('title','')!r} category={s.get('category','')}")
    except Exception as e:
        log(f"  (conv push err: {e})")


def main():
    if not OR_KEY or not OAI_KEY:
        log("missing keys (OPENROUTER + OPENAI)"); return
    log(f"── observer → {DAEMON}, ~{DUR//60}min, {CHUNK}s chunks, close-on-{SILENT_CHUNKS_TO_CLOSE}-silent ──")

    buffer: list[tuple[float, str]] = []
    conv_start: float | None = None
    silent_streak = 0
    last_now = None
    recent: list[str] = []
    end = time.time() + DUR

    while time.time() < end:
        chunk_ts = time.time()
        heard = transcribe(record(CHUNK))

        if not heard or len(heard) < 8:
            silent_streak += 1
            log("· (silence/short)")
            if conv_start is not None and silent_streak >= SILENT_CHUNKS_TO_CLOSE and buffer:
                close_conversation(conv_start, buffer)
                buffer = []; conv_start = None
            continue

        silent_streak = 0
        if conv_start is None:
            conv_start = chunk_ts
        buffer.append((chunk_ts, heard))
        recent = (recent + [heard])[-4:]

        open_atoms = fetch_open_atoms(limit=20)
        atom_lines = "\n".join(
            f' - [{a["id"]}] ({a["state"]}, {a["kind"]}, owner={a["owner"]}) {a["text"]}'
            for a in open_atoms
        ) or " (none)"
        user_msg = (f"last_now: {last_now or 'null'}\n\n"
                    f"recent transcripts (oldest first):\n"
                    + "\n".join(f" - {t}" for t in recent)
                    + f"\n\nOpen working atoms:\n{atom_lines}\n\nObservation JSON:")

        try:
            obs = llm(TICK_SYSTEM, user_msg)
        except Exception as e:
            log(f"  (llm err: {e})"); continue

        # 1. push now
        now_text = (obs.get("now") or "").strip()
        same = bool(obs.get("same_as_last"))
        if now_text:
            try:
                push_now(now_text, same); last_now = now_text
            except Exception as e:
                log(f"  (now push err: {e})")

        # 2. push new atoms, mapping "new:<i>" → daemon id for supersede refs
        new_id_map = {}
        for i, na in enumerate(obs.get("new_atoms") or []):
            try:
                r = push_atom(na, evidence=heard)
                aid = r.get("id")
                new_id_map[f"new:{i}"] = aid
                log(f"   + [{aid}] {na.get('kind','?'):<10} owner={na.get('owner','?'):<10} {(na.get('text','') or '')[:70]}")
            except Exception as e:
                log(f"  (atom push err: {e})")

        # 3. push atom updates (resolve "new:<i>" supersede pointers first)
        for u in obs.get("atom_updates") or []:
            sup = u.get("superseded_by")
            if isinstance(sup, str) and sup.startswith("new:"):
                u["superseded_by"] = new_id_map.get(sup)
            try:
                r = push_atom_update(u)
                act = r.get("action_applied", u.get("action"))
                coerced = " (coerced)" if r.get("coerced") else ""
                log(f"   ⟳ [{u.get('id')}] {act}{coerced}  conf={u.get('confidence')}")
            except Exception as e:
                log(f"  (update err: {e})")

        log(f"heard: \"{heard[:90]}\"")
        log(f"   now: {now_text}")

    # exit: close any open conversation
    if conv_start is not None and buffer:
        close_conversation(conv_start, buffer)


if __name__ == "__main__":
    main()
