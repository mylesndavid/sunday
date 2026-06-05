"""delegate_coder — hire a local coding agent for long-running work.

Sunday can't (and shouldn't) hold a multi-minute coding session inside her own
agent loop: it would block the chat, blow the tool budget, and bury the user in
intermediate noise. Instead she dispatches a real CLI coding agent — Claude Code
(`claude -p`) or Codex (`codex exec`) — DETACHED against a repo, with standing
instructions to log milestones and write a final result.md. A daemon watcher
notices when a task's result.md lands and wakes Sunday so she folds the outcome
back into the conversation in her own voice.

Lifecycle of one task:

  ~/.sunday/tasks/<id>/
    meta.json     — task, repo_dir, engine, pid, started (written at spawn)
    log.txt       — the CLI's stdout+stderr (detached)
    progress.md   — one-line milestone updates the agent appends as it works
    result.md     — final summary the agent writes when fully done
    .reported     — marker the watcher touches after it wakes Sunday

The CLI is spawned with start_new_session=True so it survives Sunday's own turn
ending and even a daemon restart (it's a fully independent process group).
"""

from __future__ import annotations

import json
import os
import secrets
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any

import structlog

from sunday.config import SundayConfig
from sunday.mcp import augmented_env
from sunday.paths import sunday_home
from sunday.tools import Tool, ToolContext, ToolRegistry

log = structlog.get_logger("sunday.coder")

# How often the watcher scans for finished tasks.
_WATCH_INTERVAL_SEC = 30
# How much of result.md to put in the wake note (the full file is on disk).
_RESULT_PREVIEW_CHARS = 500


def tasks_root() -> Path:
    return sunday_home() / "tasks"


def _task_dir(task_id: str) -> Path:
    return tasks_root() / task_id


def _new_task_id() -> str:
    # short, filesystem-safe, collision-resistant enough for per-user task dirs
    return secrets.token_hex(4)


def _resolve_cli(engine: str) -> str | None:
    """Resolve the coding-agent CLI on the augmented PATH (the daemon often
    runs under a minimal launchd PATH that lacks /opt/homebrew/bin)."""
    path = augmented_env()["PATH"]
    binary = "claude" if engine == "claude" else "codex"
    return shutil.which(binary, path=path)


def _compose_prompt(task: str, task_dir: Path) -> str:
    """The user's task plus standing instructions so the agent self-reports
    progress + a final result and works without coming back to ask."""
    d = str(task_dir)
    return (
        f"{task.strip()}\n\n"
        "--- Standing instructions (from Sunday, your dispatcher) ---\n"
        f"As you work, append one-line milestone updates to {d}/progress.md "
        "(just echo lines, no fancy formatting), e.g.:\n"
        f"  echo 'Found the bug in foo.py' >> {d}/progress.md\n"
        f"This is REQUIRED even for small tasks: before you finish you MUST write "
        f"{d}/result.md summarizing what you did and where things stand (what "
        "changed, what's left, anything the user should know). Writing result.md "
        "is how Sunday knows you're done and reports back to the user — if you "
        "skip it, your work is invisible. So always end by writing result.md, "
        f"e.g.:\n"
        f"  cat > {d}/result.md <<'EOF'\n"
        "  Done: created hello.txt with 'hi'. Nothing left.\n"
        "  EOF\n"
        "Work autonomously; do not ask questions. Make reasonable decisions and "
        "keep going until the task is complete, then write result.md."
    )


def _build_argv(engine: str, cli: str, prompt: str, task_dir: Path) -> list[str]:
    if engine == "claude":
        # -p: non-interactive (print) mode; acceptEdits so it can actually
        # write files without prompting for each edit. --add-dir grants the
        # agent write access to the task dir (outside the repo) so it can write
        # progress.md/result.md there — otherwise the sandbox blocks writes
        # outside cwd and the completion signal never lands.
        return [cli, "-p", prompt, "--permission-mode", "acceptEdits",
                "--add-dir", str(task_dir)]
    # codex: `codex exec <prompt>` is the documented non-interactive form. We
    # deliberately do NOT probe `codex --help` first — executing a binary just
    # to inspect it is what tripped the macOS malware alert; resolve, then run
    # the real command only.
    return [cli, "exec", prompt]


def _pid_alive(pid: int) -> bool:
    if not pid:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # exists, just not ours to signal
    return True


def _tail(path: Path, n_chars: int = 4000, n_lines: int | None = None) -> str:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except FileNotFoundError:
        return ""
    if n_lines is not None:
        lines = text.splitlines()
        return "\n".join(lines[-n_lines:])
    return text[-n_chars:]


# ─── delegate_coder ─────────────────────────────────────────────────────────


async def _t_delegate_coder(args: dict[str, Any], ctx: ToolContext) -> Any:
    task = (args.get("task") or "").strip()
    if not task:
        return {"error": "'task' is required"}
    repo_dir_arg = (args.get("repo_dir") or "").strip()
    if not repo_dir_arg:
        return {"error": "'repo_dir' is required — the repo the coder should work in"}
    engine = (args.get("engine") or "claude").strip().lower()
    if engine not in ("claude", "codex"):
        return {"error": f"unknown engine '{engine}' (use 'claude' or 'codex')"}

    repo_dir = Path(repo_dir_arg).expanduser()
    if not repo_dir.exists() or not repo_dir.is_dir():
        return {"error": f"repo_dir does not exist or is not a directory: {repo_dir}"}

    cli = _resolve_cli(engine)
    if not cli:
        binary = "claude" if engine == "claude" else "codex"
        return {"error": (
            f"the '{engine}' CLI ('{binary}') is not installed / not on PATH. "
            "Install it before delegating coding work to it."
        )}

    task_id = _new_task_id()
    task_dir = _task_dir(task_id)
    task_dir.mkdir(parents=True, exist_ok=True)

    prompt = _compose_prompt(task, task_dir)
    argv = _build_argv(engine, cli, prompt, task_dir)

    log_file = task_dir / "log.txt"
    try:
        logfh = open(log_file, "wb")
    except OSError as exc:
        return {"error": f"could not open log file: {exc}"}

    try:
        # Detached: new session/process group so it outlives Sunday's turn and
        # even a daemon restart. stdout+stderr → log.txt. cwd = the repo.
        proc = subprocess.Popen(
            argv,
            cwd=str(repo_dir),
            stdin=subprocess.DEVNULL,
            stdout=logfh,
            stderr=subprocess.STDOUT,
            start_new_session=True,
            env=augmented_env(),
        )
    except Exception as exc:  # noqa: BLE001 — spawn boundary
        logfh.close()
        return {"error": f"failed to spawn {engine} CLI: {type(exc).__name__}: {exc}"}
    finally:
        # The child inherited the fd; the parent doesn't need it open.
        try:
            logfh.close()
        except Exception:  # noqa: BLE001
            pass

    meta = {
        "task_id": task_id,
        "task": task,
        "repo_dir": str(repo_dir),
        "engine": engine,
        "pid": proc.pid,
        "started": time.time(),
        "argv0": cli,
    }
    try:
        (task_dir / "meta.json").write_text(json.dumps(meta, indent=2), encoding="utf-8")
    except OSError as exc:
        log.warning("could not write meta.json", task_id=task_id, error=str(exc))

    log.info("delegate_coder spawned", task_id=task_id, engine=engine,
             repo_dir=str(repo_dir), pid=proc.pid)

    return {
        "task_id": task_id,
        "engine": engine,
        "repo_dir": str(repo_dir),
        "note": (
            "running detached — check_coder_task(id) for progress. The result "
            "will also come back to you automatically as a follow-up when the "
            "coder finishes; don't block on it."
        ),
    }


# ─── check_coder_task ────────────────────────────────────────────────────────


async def _t_check_coder_task(args: dict[str, Any], ctx: ToolContext) -> Any:
    task_id = (args.get("task_id") or "").strip()
    if not task_id:
        return {"error": "'task_id' is required"}
    task_dir = _task_dir(task_id)
    if not task_dir.exists():
        return {"error": f"no such task: {task_id}"}

    pid = 0
    meta_path = task_dir / "meta.json"
    if meta_path.exists():
        try:
            pid = int(json.loads(meta_path.read_text(encoding="utf-8")).get("pid") or 0)
        except Exception:  # noqa: BLE001
            pid = 0

    result_path = task_dir / "result.md"
    result = None
    if result_path.exists():
        result = result_path.read_text(encoding="utf-8", errors="replace")

    return {
        "task_id": task_id,
        "running": _pid_alive(pid),
        "done": result is not None,
        "progress": _tail(task_dir / "progress.md") or "(no milestones yet)",
        "result": result,
        "log_tail": _tail(task_dir / "log.txt", n_lines=15) or "(log empty)",
    }


# ─── watcher (daemon background task) ────────────────────────────────────────


async def _watch_finished_tasks(daemon: Any) -> None:
    """Every ~30s scan ~/.sunday/tasks/*/ for a result.md that hasn't been
    reported yet; on a hit, touch .reported and wake Sunday with a short note
    so she tells the user (in her own voice) that the coder finished."""
    import asyncio

    inject = getattr(daemon, "_inject_and_wake", None)
    root = tasks_root()
    while True:
        try:
            await asyncio.sleep(_WATCH_INTERVAL_SEC)
            if not root.exists():
                continue
            for task_dir in root.iterdir():
                if not task_dir.is_dir():
                    continue
                result_path = task_dir / "result.md"
                reported = task_dir / ".reported"
                if not result_path.exists():
                    continue
                # Already reported, and the result hasn't been rewritten since?
                if reported.exists() and reported.stat().st_mtime >= result_path.stat().st_mtime:
                    continue
                reported.touch()
                preview = _tail(result_path, n_chars=_RESULT_PREVIEW_CHARS)
                task_id = task_dir.name
                engine = "?"
                meta_path = task_dir / "meta.json"
                if meta_path.exists():
                    try:
                        engine = json.loads(meta_path.read_text(encoding="utf-8")).get("engine", "?")
                    except Exception:  # noqa: BLE001
                        pass
                note = (
                    f"[delegate_coder task {task_id} finished — engine: {engine}]\n\n"
                    f"result (first {_RESULT_PREVIEW_CHARS} chars):\n{preview}\n\n"
                    f"Report this to the user in your own voice. The full result "
                    f"is at {result_path}; use check_coder_task('{task_id}') if you "
                    f"need progress.md or the log."
                )
                log.info("delegate_coder finished — waking", task_id=task_id, engine=engine)
                if inject is not None:
                    try:
                        await inject(note)
                    except Exception as exc:  # noqa: BLE001
                        log.warning("delegate_coder wake inject failed", task_id=task_id, error=str(exc))
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 — watcher must never die
            log.warning("delegate_coder watcher iteration failed", error=str(exc))


# ─── registration ────────────────────────────────────────────────────────────


def register(registry: ToolRegistry, config: SundayConfig) -> None:
    # Register the watcher as a daemon background task. Imported lazily so the
    # module is importable outside daemon scope (tests, scripts).
    try:
        from sunday.daemon import register_background_task
        register_background_task(_watch_finished_tasks)
    except Exception as exc:  # noqa: BLE001 — best-effort; tools still register
        log.warning("delegate_coder watcher not registered", error=str(exc))

    registry.register(Tool(
        name="delegate_coder",
        description=(
            "Hire a local coding agent (Claude Code or Codex CLI) to do "
            "long-running coding work in a repo on this Mac — detached, "
            "milestone-tracked, completion folds back into the chat. Use this "
            "for real software tasks (implement a feature, fix a bug across "
            "files, refactor, write tests) that take minutes and shouldn't block "
            "the conversation. ONE repo per task. The coder EDITS that repo "
            "(irreversible-ish), so unless the user explicitly asked you to run "
            "it, CONFIRM with them first. ANNOUNCE the kickoff in your reply — "
            "say what you're having it do and which repo — then call this. It "
            "returns immediately with a task_id; the result comes back to you "
            "later as a follow-up, so don't wait. Use check_coder_task(id) to "
            "peek at progress. engine defaults to 'claude'."
        ),
        parameters={
            "type": "object",
            "properties": {
                "task": {
                    "type": "string",
                    "description": (
                        "Self-contained coding task for the agent. Include "
                        "everything it needs — it only sees this and the repo, "
                        "not the chat. Be specific about the desired outcome."
                    ),
                },
                "repo_dir": {
                    "type": "string",
                    "description": "Absolute path to the repo/dir the coder should work in. Must exist.",
                },
                "engine": {
                    "type": "string",
                    "enum": ["claude", "codex"],
                    "description": "Which coding CLI to hire. Default 'claude' (Claude Code).",
                },
            },
            "required": ["task", "repo_dir"],
        },
        run=_t_delegate_coder,
    ))

    registry.register(Tool(
        name="check_coder_task",
        description=(
            "Check on a delegate_coder task by its id: whether the coder is "
            "still running, the tail of its progress.md milestones, the final "
            "result.md if it's done, and the last ~15 lines of its log. Use it "
            "when the user asks how the coding work is going, or before "
            "reporting a result."
        ),
        parameters={
            "type": "object",
            "properties": {
                "task_id": {"type": "string", "description": "The id returned by delegate_coder."},
            },
            "required": ["task_id"],
        },
        run=_t_check_coder_task,
    ))
