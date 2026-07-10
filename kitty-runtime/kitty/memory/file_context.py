from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(slots=True, frozen=True)
class FileContext:
    """Read-only project guidance and long-term memory context."""

    agents_guidance: str = ""
    memory: str = ""

    @classmethod
    def load(cls, project_root: str | Path, max_chars_per_file: int = 20_000) -> "FileContext":
        root = Path(project_root).expanduser().resolve()
        return cls(
            agents_guidance=_read_limited(root / "AGENTS.md", max_chars_per_file),
            memory=_read_limited(root / "MEMORY.md", max_chars_per_file),
        )

    def render(self) -> str:
        blocks = []
        if self.agents_guidance:
            blocks.append("# Project guidance\n" + self.agents_guidance)
        if self.memory:
            blocks.append("# Project memory\n" + self.memory)
        return "\n\n".join(blocks)


def _read_limited(path: Path, max_chars: int) -> str:
    if not path.is_file():
        return ""
    return path.read_text(encoding="utf-8", errors="replace")[:max_chars]
