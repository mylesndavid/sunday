"""Sunday's skill library.

A "skill" is a markdown file under ~/.sunday/skills/ that describes how
to do a specific task (draft a thoughtful follow-up, run a deep research
loop, prep talking points for a call, etc.). Sunday picks them off the
shelf when relevant — `list_skills` to see what's available,
`load_skill` to pull one into context for the rest of the turn.

This is the lightweight version of what Hermes does natively: text files
on disk, no framework, model decides what to load. Bring your own skills
(or none) and they Just Work.

File format:
  ~/.sunday/skills/<slug>.md

The first line, if it starts with `#`, becomes the human-friendly name.
Lines 2+ are the body. Empty front-matter is fine.

Example:
  # Draft a follow-up email
  When the user asks for a follow-up email, do this:
  - Read the prior thread if any (use imessage_read_thread / gmail).
  - Match their voice and length.
  - Default to two sentences.
  - End with one specific ask.
"""

from __future__ import annotations

import io
import re
import tarfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
import structlog

from sunday.config import SundayConfig
from sunday.paths import sunday_home
from sunday.tools import Tool, ToolContext, ToolRegistry

log = structlog.get_logger("sunday.skills")

# skills.sh — the open Agent Skills directory (vercel-labs/skills). Search
# returns entries with id="<owner>/<repo>/<skillId>" where skillId matches
# the frontmatter `name:` of a SKILL.md somewhere in the repo (not
# necessarily at /skillId/SKILL.md). Install pulls the repo tarball,
# scans for SKILL.md, and matches by frontmatter name.
SKILLS_SH_SEARCH    = "https://skills.sh/api/search"
GITHUB_TARBALL      = "https://codeload.github.com"  # no rate limit
_BRANCH_CANDIDATES  = ("main", "master")
_SKILL_FILENAMES    = ("SKILL.md", "skill.md")
_MAX_TARBALL_BYTES  = 64 * 1024 * 1024   # 64 MB sanity cap


def skills_dir() -> Path:
    d = sunday_home() / "skills"
    d.mkdir(parents=True, exist_ok=True)
    return d


@dataclass(slots=True)
class Skill:
    slug: str           # filename without .md
    name: str           # human-friendly title (first H1 if present, else slug)
    description: str    # first non-title paragraph (best-effort one-liner)
    path: Path

    def body(self) -> str:
        return self.path.read_text(encoding="utf-8")


def _parse(path: Path) -> Skill:
    text = path.read_text(encoding="utf-8")
    name = path.stem.replace("-", " ").replace("_", " ").title()
    description = ""

    # Skills installed from skills.sh / Anthropic carry YAML frontmatter
    # (--- name: … description: … ---). Honour it the way Hermes does: the
    # frontmatter fields win, and we scan the BODY (not the delimiter line)
    # for fallbacks. Without this, the shelf showed name="Frontend Design",
    # description="---" — the delimiter leaking in as the one-liner.
    fm_name, fm_desc, body = _split_frontmatter(text)
    if fm_name:
        name = fm_name
    if fm_desc:
        description = fm_desc

    lines = body.splitlines()
    # First # heading becomes the name (unless frontmatter already set one);
    # the first non-blank line after it becomes the description fallback.
    if lines and lines[0].lstrip().startswith("#"):
        if not fm_name:
            name = lines[0].lstrip("#").strip() or name
        if not description:
            for line in lines[1:]:
                if line.strip():
                    description = line.strip()
                    break
    elif lines and not description:
        description = next((l.strip() for l in lines if l.strip()), "")
    return Skill(slug=path.stem, name=name, description=description[:240], path=path)


def _split_frontmatter(text: str) -> tuple[str | None, str | None, str]:
    """Return (name, description, body) — name/description pulled from a leading
    YAML frontmatter block if present, body being the markdown after it.
    Returns (None, None, text) when there's no frontmatter."""
    m = _FRONTMATTER_RE.match(text)
    if not m:
        return None, None, text
    name = desc = None
    for line in m.group(1).splitlines():
        s = line.strip()
        if s.startswith("name:") and name is None:
            name = s.split(":", 1)[1].strip().strip("\"'") or None
        elif s.startswith("description:") and desc is None:
            desc = s.split(":", 1)[1].strip().strip("\"'") or None
    return name, desc, text[m.end():]


def list_skills() -> list[Skill]:
    return sorted(
        (_parse(p) for p in skills_dir().glob("*.md") if p.is_file()),
        key=lambda s: s.slug,
    )


def load_skill(slug: str) -> Skill | None:
    p = skills_dir() / f"{slug}.md"
    if not p.exists():
        return None
    return _parse(p)


def delete_skill(slug: str) -> bool:
    """Remove a skill file. Returns True if it existed and was deleted."""
    p = skills_dir() / f"{slug}.md"
    if not p.exists():
        return False
    p.unlink()
    log.info("skill deleted", slug=slug)
    return True


# How many skills to name on the shelf before the model would rather call
# list_skills than read another line. Generous — the shelf is one line each
# and only the description is truncated, so even a big library stays cheap.
_SHELF_CAP = 60
# Per-skill description budget on the shelf. Long enough to know if a skill
# is relevant, short enough that 60 of them don't bloat the turn. Mirrors
# Hermes's 60-char truncation in extract_skill_description.
_SHELF_DESC_CHARS = 80


def skills_shelf() -> str:
    """The 'skills on the shelf' block injected into Sunday's per-turn context.

    This is the awareness mechanism, lifted from Hermes (agent/prompt_builder.py
    build_skills_system_prompt): the model SEES the full index of installed
    skills every turn — name + one-line description — so it calls load_skill
    when one's relevant without having to remember list_skills exists. Hermes
    proves this is the difference between skills that get used and skills that
    rot on disk.

    Returns "" when no skills are installed (nothing to advertise). Kept out of
    the cached system prefix and folded into the per-turn context block instead,
    so the prompt cache stays warm even as the library changes — same reason
    memory.core_block() rides the per-turn block, not stable_prefix().
    """
    skills = list_skills()
    if not skills:
        return ""
    lines = []
    for s in skills[:_SHELF_CAP]:
        desc = " ".join(s.description.split())  # collapse newlines/runs
        if len(desc) > _SHELF_DESC_CHARS:
            desc = desc[: _SHELF_DESC_CHARS - 1].rstrip() + "…"
        lines.append(f"- {s.slug}: {desc}" if desc else f"- {s.slug}")
    more = ""
    if len(skills) > _SHELF_CAP:
        more = f"\n…and {len(skills) - _SHELF_CAP} more — list_skills to see them all."
    return (
        "Skills on the shelf — procedures you already know how to run. Before you "
        "work through a task by hand, scan this list: if one matches or is even "
        "partly relevant, load_skill(slug) and follow it. They hold the exact "
        "steps, selectors, and gotchas that beat figuring it out fresh, plus the "
        "way the user wants the task done. Loading one you don't end up needing "
        "costs nothing; skipping one that fit costs a worse answer. Nothing here "
        "for the task? search_skills checks the open directory of community "
        "skills and install_skill adds one in seconds — worth a look before "
        "improvising a multi-step procedure.\n\n"
        + "\n".join(lines) + more
    )


# ─── skills.sh directory ─────────────────────────────────────────────────


async def search_directory(query: str, limit: int = 10) -> list[dict[str, Any]]:
    """Search the open skills.sh directory. Returns one entry per skill
    with `id` (the full `<owner>/<repo>/<skillDir>` slug), `name`, `source`
    (the GitHub repo), and `installs` count."""
    if not query.strip():
        return []
    async with httpx.AsyncClient(timeout=15) as client:
        res = await client.get(SKILLS_SH_SEARCH, params={"q": query, "limit": limit})
    if res.status_code >= 400:
        raise RuntimeError(f"skills.sh {res.status_code}: {res.text[:200]}")
    data = res.json()
    out = []
    for s in data.get("skills") or []:
        out.append({
            "id":       s.get("id"),
            "name":     s.get("name") or s.get("skillId"),
            "skill_id": s.get("skillId"),
            "source":   s.get("source"),
            "installs": s.get("installs", 0),
            "url":      f"https://skills.sh/{s.get('id')}",
        })
    return out


def _slug_safe(name: str) -> str:
    s = re.sub(r"[^a-zA-Z0-9_-]+", "-", name.strip().lower())
    return re.sub(r"-+", "-", s).strip("-") or "skill"


_FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n", re.DOTALL)


def _parse_frontmatter_name(body: str) -> str | None:
    m = _FRONTMATTER_RE.match(body)
    if not m:
        return None
    for line in m.group(1).splitlines():
        line = line.strip()
        if line.startswith("name:"):
            val = line.split(":", 1)[1].strip().strip("\"'")
            return val or None
    return None


async def _download_tarball(owner: str, repo: str) -> tuple[str, bytes]:
    """Try main then master. Returns (branch, raw bytes)."""
    last_status = 0
    async with httpx.AsyncClient(timeout=60, follow_redirects=True) as client:
        for branch in _BRANCH_CANDIDATES:
            url = f"{GITHUB_TARBALL}/{owner}/{repo}/tar.gz/{branch}"
            res = await client.get(url)
            if res.status_code == 200:
                if len(res.content) > _MAX_TARBALL_BYTES:
                    raise RuntimeError(f"repo tarball is {len(res.content)//1024//1024} MB — over 64 MB cap")
                return branch, res.content
            last_status = res.status_code
    raise FileNotFoundError(f"could not download {owner}/{repo} tarball (last HTTP {last_status})")


def _walk_tarball_for_skills(raw: bytes) -> list[dict[str, Any]]:
    """Return one entry per SKILL.md/skill.md found, with the frontmatter
    name (if any) and the file body."""
    out = []
    with tarfile.open(fileobj=io.BytesIO(raw), mode="r:gz") as tf:
        for m in tf.getmembers():
            if not m.isfile():
                continue
            base = m.name.rsplit("/", 1)[-1]
            if base not in _SKILL_FILENAMES:
                continue
            f = tf.extractfile(m)
            if f is None:
                continue
            body = f.read().decode("utf-8", errors="replace")
            # `m.name` is "<repo>-<sha>/path/to/SKILL.md" — strip the leading
            # archive root so the path is repo-relative.
            rel = m.name.split("/", 1)[1] if "/" in m.name else m.name
            out.append({
                "path":             rel,
                "frontmatter_name": _parse_frontmatter_name(body),
                "body":             body,
            })
    return out


async def install_from_directory(slug: str) -> dict[str, Any]:
    """Install a skill by its skills.sh id (`<owner>/<repo>/<skillId>`) or
    `<owner>/<repo>` for the first skill in a single-skill repo. Downloads
    the repo tarball, finds the matching SKILL.md by frontmatter name (or
    directory name if no frontmatter), and writes it to
    ~/.sunday/skills/<local-slug>.md."""
    parts = slug.strip().strip("/").split("/")
    if len(parts) < 2:
        raise ValueError(
            f"slug must look like '<owner>/<repo>' or '<owner>/<repo>/<skillId>', got {slug!r}"
        )
    owner, repo, *rest = parts
    wanted = rest[-1] if rest else None

    branch, raw = await _download_tarball(owner, repo)
    found = _walk_tarball_for_skills(raw)
    if not found:
        raise FileNotFoundError(f"no SKILL.md anywhere in {owner}/{repo}@{branch}")

    chosen = None
    if wanted:
        # Prefer frontmatter name match; fall back to directory-name match.
        chosen = next((s for s in found if s["frontmatter_name"] == wanted), None)
        if chosen is None:
            chosen = next(
                (s for s in found if s["path"].split("/")[-2:-1] == [wanted]),
                None,
            )
        if chosen is None:
            available = [s["frontmatter_name"] or s["path"] for s in found][:8]
            raise FileNotFoundError(
                f"skill '{wanted}' not found in {owner}/{repo}. "
                f"Available: {', '.join(available)}"
            )
    else:
        chosen = found[0]

    local_slug = _slug_safe(chosen["frontmatter_name"] or wanted or chosen["path"].split("/")[-2] or repo)
    target = skills_dir() / f"{local_slug}.md"
    target.write_text(chosen["body"], encoding="utf-8")
    log.info(
        "skill installed",
        slug=slug,
        local_slug=local_slug,
        source=f"{owner}/{repo}@{branch}:{chosen['path']}",
        bytes=len(chosen["body"]),
    )
    return {
        "ok":         True,
        "slug":       local_slug,
        "source":     f"{owner}/{repo}@{branch}:{chosen['path']}",
        "path":       str(target),
        "size_bytes": len(chosen["body"]),
    }


# ─── tools ───────────────────────────────────────────────────────────────


async def _t_list_skills(args: dict[str, Any], ctx: ToolContext) -> Any:
    skills = list_skills()
    if not skills:
        return {
            "skills": [],
            "note": (
                f"No skills installed yet. Drop markdown files in {skills_dir()} "
                "to teach Sunday reusable procedures."
            ),
        }
    return {
        "skills": [
            {"slug": s.slug, "name": s.name, "description": s.description}
            for s in skills
        ],
    }


async def _t_load_skill(args: dict[str, Any], ctx: ToolContext) -> Any:
    slug = (args.get("slug") or "").strip()
    if not slug:
        return {"error": "'slug' is required"}
    skill = load_skill(slug)
    if skill is None:
        return {
            "error": (
                f"no such skill: {slug}. Use list_skills to see what's "
                "installed, or search_skills + install_skill to pull one "
                "from the skills.sh directory."
            )
        }
    return {"slug": skill.slug, "name": skill.name, "body": skill.body()}


async def _t_search_directory(args: dict[str, Any], ctx: ToolContext) -> Any:
    query = (args.get("query") or "").strip()
    if not query:
        return {"error": "'query' is required"}
    limit = max(1, min(int(args.get("limit") or 10), 25))
    try:
        skills = await search_directory(query, limit=limit)
    except Exception as exc:  # noqa: BLE001
        return {"error": f"{type(exc).__name__}: {exc}"}
    return {"results": skills, "directory": "https://skills.sh"}


async def _t_install_skill(args: dict[str, Any], ctx: ToolContext) -> Any:
    slug = (args.get("slug") or "").strip()
    if not slug:
        return {"error": "'slug' is required (e.g. 'anthropics/skills/frontend-design')"}
    try:
        return await install_from_directory(slug)
    except FileNotFoundError as exc:
        return {"error": str(exc)}
    except Exception as exc:  # noqa: BLE001
        return {"error": f"{type(exc).__name__}: {exc}"}


async def _t_save_skill(args: dict[str, Any], ctx: ToolContext) -> Any:
    """Sunday can write her own skills — extracted from prior conversations.

    Use sparingly: this is a procedure she wants to remember HOW to do, not
    a fact about the user (that's `remember`).
    """
    slug = (args.get("slug") or "").strip().lower().replace(" ", "-")
    body = args.get("body") or ""
    if not slug or not body:
        return {"error": "'slug' and 'body' are required"}
    if not all(c.isalnum() or c in "-_" for c in slug):
        return {"error": "slug must be alphanumeric / dashes / underscores only"}
    p = skills_dir() / f"{slug}.md"
    p.write_text(body, encoding="utf-8")
    log.info("skill saved", slug=slug, bytes=len(body))
    return {"ok": True, "slug": slug, "path": str(p)}


def register(registry: ToolRegistry, config: SundayConfig) -> None:
    registry.register(Tool(
        name="list_skills",
        description=(
            "List every installed skill with its full description. The skill "
            "shelf you already see each turn is the short index; call this only "
            "when you need the longer descriptions, or when the shelf was capped "
            "and said there were more. Each entry has a slug, name, and "
            "description — then load_skill(slug) to pull one into context."
        ),
        parameters={"type": "object", "properties": {}},
        run=_t_list_skills,
    ))
    registry.register(Tool(
        name="load_skill",
        description=(
            "Load a specific skill's full procedure into context. Use after "
            "list_skills when one looks relevant. The body becomes part of "
            "what you can act on for the rest of this turn."
        ),
        parameters={
            "type": "object",
            "properties": {"slug": {"type": "string"}},
            "required": ["slug"],
        },
        run=_t_load_skill,
    ))
    registry.register(Tool(
        name="search_skills",
        description=(
            "Search the open skills.sh directory for a reusable procedure "
            "before reinventing one. Returns up to 10 skills sorted by "
            "popularity, each with a slug like '<owner>/<repo>/<skillDir>'. "
            "Pair with install_skill to pull one onto disk, then load_skill "
            "to use it."
        ),
        parameters={
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Free-text search (e.g. 'remotion', 'gmail triage')."},
                "limit": {"type": "integer", "default": 10},
            },
            "required": ["query"],
        },
        run=_t_search_directory,
    ))
    registry.register(Tool(
        name="install_skill",
        description=(
            "Download a skill from skills.sh by its slug "
            "('<owner>/<repo>/<skillDir>' or just '<owner>/<repo>'). Fetches "
            "SKILL.md from GitHub raw, writes it under ~/.sunday/skills/, "
            "and returns the local slug you can pass to load_skill."
        ),
        parameters={
            "type": "object",
            "properties": {
                "slug": {
                    "type": "string",
                    "description": (
                        "Full skills.sh id like 'anthropics/skills/frontend-design'. "
                        "Get this from search_skills."
                    ),
                },
            },
            "required": ["slug"],
        },
        run=_t_install_skill,
    ))
    registry.register(Tool(
        name="save_skill",
        description=(
            "Write a skill to disk — a reusable procedure Sunday should "
            "remember HOW to do (not a fact about the user; use `remember` "
            "for that). Slug becomes the filename; reusing an existing slug "
            "OVERWRITES it, which is how you patch a skill you found outdated "
            "or wrong. Body should read like a short instruction sheet — a "
            "tight numbered procedure with the exact tools, selectors, and "
            "gotchas. First H1 becomes the name."
        ),
        parameters={
            "type": "object",
            "properties": {
                "slug": {"type": "string", "description": "Short kebab-case identifier."},
                "body": {"type": "string", "description": "Markdown procedure. First H1 becomes the name."},
            },
            "required": ["slug", "body"],
        },
        run=_t_save_skill,
    ))
