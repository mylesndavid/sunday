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

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import structlog

from sunday.config import SundayConfig
from sunday.paths import sunday_home
from sunday.tools import Tool, ToolContext, ToolRegistry

log = structlog.get_logger("sunday.skills")


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
    lines = text.splitlines()
    name = path.stem.replace("-", " ").replace("_", " ").title()
    description = ""
    # First # heading becomes the name; the first paragraph after becomes
    # the description.
    if lines and lines[0].startswith("#"):
        name = lines[0].lstrip("#").strip() or name
        for line in lines[1:]:
            if line.strip():
                description = line.strip()
                break
    elif lines:
        description = next((l.strip() for l in lines if l.strip()), "")
    return Skill(slug=path.stem, name=name, description=description[:240], path=path)


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
        return {"error": f"no such skill: {slug}. Use list_skills to see what's installed."}
    return {"slug": skill.slug, "name": skill.name, "body": skill.body()}


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
            "List Sunday's installed skills — reusable procedures she's been "
            "taught (or has taught herself). Each skill has a slug, name, and "
            "short description. Use this when starting a task that might "
            "match an existing procedure; then load_skill(slug) to pull it "
            "into context."
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
        name="save_skill",
        description=(
            "Write a new skill to disk — a reusable procedure Sunday should "
            "remember HOW to do (not a fact about the user; use `remember` "
            "for that). Slug becomes the filename. Body should read like a "
            "short instruction sheet."
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
