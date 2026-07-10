from __future__ import annotations

import ast
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable


@dataclass(slots=True, frozen=True)
class SkillDefinition:
    name: str
    description: str
    triggers: tuple[str, ...]
    body: str
    path: Path


class SkillCatalog:
    """Discovers SKILL.md packages without requiring a YAML dependency."""

    def __init__(self, skills: Iterable[SkillDefinition] = ()):
        self.skills = tuple(skills)

    @classmethod
    def discover(cls, root: str | Path) -> "SkillCatalog":
        root_path = Path(root).expanduser().resolve()
        paths = sorted(root_path.glob(".agents/*/skills/*/SKILL.md"))
        paths.extend(sorted(root_path.glob("skills/*/SKILL.md")))
        seen: set[Path] = set()
        skills: list[SkillDefinition] = []
        for path in paths:
            if path in seen:
                continue
            seen.add(path)
            skills.append(_parse_skill(path))
        return cls(skills)

    def select(self, query: str) -> list[SkillDefinition]:
        lowered = query.casefold()
        selected: list[SkillDefinition] = []
        for skill in self.skills:
            terms = (skill.name, *skill.triggers)
            if any(term and term.casefold() in lowered for term in terms):
                selected.append(skill)
        return selected

    @staticmethod
    def render_context(skills: Iterable[SkillDefinition]) -> str:
        blocks = []
        for skill in skills:
            blocks.append(f"## Skill: {skill.name}\n{skill.description}\n\n{skill.body.strip()}")
        return "\n\n".join(blocks)


def _parse_skill(path: Path) -> SkillDefinition:
    text = path.read_text(encoding="utf-8")
    metadata: dict[str, str] = {}
    body = text
    if text.startswith("---"):
        parts = text.split("---", 2)
        if len(parts) == 3:
            _, raw_meta, body = parts
            for line in raw_meta.splitlines():
                if ":" not in line or line.startswith((" ", "\t")):
                    continue
                key, value = line.split(":", 1)
                metadata[key.strip()] = value.strip().strip("'\"")

    name = metadata.get("name") or path.parent.name
    description = metadata.get("description", "")
    triggers = _parse_triggers(metadata.get("triggers", ""))
    return SkillDefinition(name, description, triggers, body.strip(), path)


def _parse_triggers(raw: str) -> tuple[str, ...]:
    if not raw:
        return ()
    if raw.startswith("["):
        try:
            value = ast.literal_eval(raw)
            if isinstance(value, list):
                return tuple(str(item).strip() for item in value if str(item).strip())
        except (SyntaxError, ValueError):
            # Accept the common lightweight YAML form: [screenshot, 截图].
            raw = raw[1:-1] if raw.endswith("]") else raw[1:]
    return tuple(
        item.strip().strip("'\"")
        for item in raw.split(",")
        if item.strip().strip("'\"")
    )
