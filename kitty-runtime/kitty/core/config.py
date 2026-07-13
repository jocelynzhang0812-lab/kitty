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
    tool_executor: str = "in_process"
    tool_denylist: tuple[str, ...] = ()
    tool_max_output_bytes: int = 65536
    tool_container_image: str = ""
    tool_container_workspace: str = ""
    tool_container_network: str = "none"
    tool_container_memory: str = "256m"
    tool_container_cpus: str = "1"
    tool_container_pids_limit: int = 128
    tool_container_tmpfs_size: str = "64m"
    tool_container_readonly_mounts: tuple[str, ...] = ()
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
        if self.tool_executor not in {"in_process", "subprocess", "container"}:
            raise ValueError("tool_executor must be in_process, subprocess, or container")
        if self.tool_max_output_bytes < 1024:
            raise ValueError("tool_max_output_bytes must be at least 1024")
        if self.tool_executor == "container" and not self.tool_container_image:
            raise ValueError("tool_container_image is required when tool_executor is container")
        if self.tool_container_pids_limit < 1:
            raise ValueError("tool_container_pids_limit must be positive")

    @classmethod
    def from_env(cls) -> "KittyConfig":
        state_dir = Path(os.getenv("KITTY_STATE_DIR", ".kitty"))
        workspace = os.getenv("KITTY_WORKSPACE_ROOT")
        return cls(
            state_dir=state_dir,
            workspace_root=Path(workspace) if workspace else None,
            hook_timeout_seconds=float(os.getenv("KITTY_HOOK_TIMEOUT", "10")),
            tool_timeout_seconds=float(os.getenv("KITTY_TOOL_TIMEOUT", "30")),
            tool_executor=os.getenv("KITTY_TOOL_EXECUTOR", "in_process").strip(),
            tool_denylist=_csv_env("KITTY_TOOL_DENYLIST"),
            tool_max_output_bytes=int(os.getenv("KITTY_TOOL_MAX_OUTPUT_BYTES", "65536")),
            tool_container_image=os.getenv("KITTY_TOOL_CONTAINER_IMAGE", "").strip(),
            tool_container_workspace=os.getenv("KITTY_TOOL_CONTAINER_WORKSPACE", "").strip(),
            tool_container_network=os.getenv("KITTY_TOOL_CONTAINER_NETWORK", "none").strip(),
            tool_container_memory=os.getenv("KITTY_TOOL_CONTAINER_MEMORY", "256m").strip(),
            tool_container_cpus=os.getenv("KITTY_TOOL_CONTAINER_CPUS", "1").strip(),
            tool_container_pids_limit=int(
                os.getenv("KITTY_TOOL_CONTAINER_PIDS_LIMIT", "128")
            ),
            tool_container_tmpfs_size=os.getenv(
                "KITTY_TOOL_CONTAINER_TMPFS_SIZE", "64m"
            ).strip(),
            tool_container_readonly_mounts=_csv_env("KITTY_TOOL_CONTAINER_READONLY_MOUNTS"),
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


def _csv_env(name: str) -> tuple[str, ...]:
    return tuple(item.strip() for item in os.getenv(name, "").split(",") if item.strip())
