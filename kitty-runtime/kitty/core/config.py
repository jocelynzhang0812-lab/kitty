from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path


@dataclass(slots=True)
class KittyConfig:
    """Runtime settings with secret-free environment loading."""

    state_dir: Path = field(default_factory=lambda: Path(".kitty"))
    workspace_root: Path | None = None
    hook_timeout_seconds: float = 10.0
    tool_timeout_seconds: float = 30.0
    max_agent_steps: int = 8
    stream_chunk_size: int = 80
    system_prompt: str = "You are a helpful agent running inside Kitty."

    def __post_init__(self) -> None:
        self.state_dir = Path(self.state_dir).expanduser().resolve()
        if self.workspace_root is None:
            self.workspace_root = self.state_dir / "workspaces"
        else:
            self.workspace_root = Path(self.workspace_root).expanduser().resolve()
        if self.max_agent_steps < 1:
            raise ValueError("max_agent_steps must be at least 1")
        if self.stream_chunk_size < 1:
            raise ValueError("stream_chunk_size must be at least 1")

    @classmethod
    def from_env(cls) -> "KittyConfig":
        state_dir = Path(os.getenv("KITTY_STATE_DIR", ".kitty"))
        workspace = os.getenv("KITTY_WORKSPACE_ROOT")
        return cls(
            state_dir=state_dir,
            workspace_root=Path(workspace) if workspace else None,
            hook_timeout_seconds=float(os.getenv("KITTY_HOOK_TIMEOUT", "10")),
            tool_timeout_seconds=float(os.getenv("KITTY_TOOL_TIMEOUT", "30")),
            max_agent_steps=int(os.getenv("KITTY_MAX_AGENT_STEPS", "8")),
            stream_chunk_size=int(os.getenv("KITTY_STREAM_CHUNK_SIZE", "80")),
            system_prompt=os.getenv(
                "KITTY_SYSTEM_PROMPT",
                "You are a helpful agent running inside Kitty.",
            ),
        )

    @property
    def session_db_path(self) -> Path:
        return self.state_dir / "sessions.db"

    @property
    def log_dir(self) -> Path:
        return self.state_dir / "logs"

    def ensure_dirs(self) -> None:
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.workspace_root.mkdir(parents=True, exist_ok=True)
        self.log_dir.mkdir(parents=True, exist_ok=True)
