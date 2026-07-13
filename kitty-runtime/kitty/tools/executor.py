from __future__ import annotations

import asyncio
import inspect
import json
import os
import sys
from collections.abc import Iterable, Mapping
from dataclasses import asdict, dataclass, is_dataclass
from typing import Any, Protocol


@dataclass(slots=True, frozen=True)
class ToolExecution:
    name: str
    ok: bool
    output: Any = None
    error: str = ""

    def model_payload(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "output": _jsonable(self.output),
            "error": self.error,
        }


@dataclass(slots=True, frozen=True)
class ToolPolicy:
    """Runtime policy enforced before a tool handler is invoked."""

    denylist: frozenset[str] = frozenset()

    @classmethod
    def from_env(cls) -> "ToolPolicy":
        return cls(denylist=frozenset(_csv_env("KITTY_TOOL_DENYLIST")))

    def check(self, name: str) -> str:
        if name in self.denylist:
            return "tool is denied by policy"
        return ""


class ToolExecutor(Protocol):
    async def execute(
        self,
        spec: Any,
        arguments: Mapping[str, Any],
        timeout_seconds: float,
    ) -> ToolExecution:
        ...


class InProcessToolExecutor:
    """Executes tools in the agent process using the existing thread offload path."""

    async def execute(
        self,
        spec: Any,
        arguments: Mapping[str, Any],
        timeout_seconds: float,
    ) -> ToolExecution:
        try:
            if inspect.iscoroutinefunction(spec.handler):
                value = await asyncio.wait_for(spec.handler(**arguments), timeout=timeout_seconds)
            else:
                value = await asyncio.wait_for(
                    asyncio.to_thread(spec.handler, **arguments),
                    timeout=timeout_seconds,
                )
                if inspect.isawaitable(value):
                    value = await asyncio.wait_for(value, timeout=timeout_seconds)
            return ToolExecution(name=spec.name, ok=True, output=value)
        except TimeoutError:
            return ToolExecution(name=spec.name, ok=False, error="tool execution timed out")
        except Exception as exc:
            return ToolExecution(name=spec.name, ok=False, error=f"{type(exc).__name__}: {exc}")


class SubprocessToolExecutor:
    """Executes importable tools in a child Python process with a hard timeout."""

    def __init__(
        self,
        *,
        python_executable: str | None = None,
        max_output_bytes: int = 65536,
    ):
        if max_output_bytes < 1024:
            raise ValueError("max_output_bytes must be at least 1024")
        self.python_executable = python_executable or sys.executable
        self.max_output_bytes = max_output_bytes

    async def execute(
        self,
        spec: Any,
        arguments: Mapping[str, Any],
        timeout_seconds: float,
    ) -> ToolExecution:
        handler_ref = getattr(spec, "handler_ref", "") or infer_handler_ref(spec.handler)
        if not handler_ref:
            return ToolExecution(
                name=spec.name,
                ok=False,
                error="tool is not subprocess-capable; provide handler_ref='module:function'",
            )
        payload = json.dumps(
            {
                "name": spec.name,
                "handler_ref": handler_ref,
                "arguments": dict(arguments),
                "max_output_bytes": self.max_output_bytes,
            },
            ensure_ascii=False,
        ).encode("utf-8")
        process = await asyncio.create_subprocess_exec(
            self.python_executable,
            "-m",
            "kitty.tools.subprocess_runner",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, stderr = await asyncio.wait_for(
                process.communicate(payload),
                timeout=timeout_seconds,
            )
        except TimeoutError:
            process.kill()
            await process.wait()
            return ToolExecution(name=spec.name, ok=False, error="tool execution timed out")

        return _decode_runner_result(
            spec.name,
            process.returncode,
            stdout,
            stderr,
            self.max_output_bytes,
        )


@dataclass(slots=True, frozen=True)
class ContainerSandboxConfig:
    image: str
    workspace_root: str = ""
    python_executable: str = "python"
    network: str = "none"
    memory: str = "256m"
    cpus: str = "1"
    pids_limit: int = 128
    tmpfs_size: str = "64m"
    extra_readonly_mounts: tuple[str, ...] = ()

    @classmethod
    def from_env(cls) -> "ContainerSandboxConfig":
        mounts = tuple(_csv_env("KITTY_TOOL_CONTAINER_READONLY_MOUNTS"))
        return cls(
            image=os.getenv("KITTY_TOOL_CONTAINER_IMAGE", "").strip(),
            workspace_root=os.getenv("KITTY_TOOL_CONTAINER_WORKSPACE", "").strip(),
            python_executable=os.getenv("KITTY_TOOL_CONTAINER_PYTHON", "python").strip()
            or "python",
            network=os.getenv("KITTY_TOOL_CONTAINER_NETWORK", "none").strip() or "none",
            memory=os.getenv("KITTY_TOOL_CONTAINER_MEMORY", "256m").strip() or "256m",
            cpus=os.getenv("KITTY_TOOL_CONTAINER_CPUS", "1").strip() or "1",
            pids_limit=int(os.getenv("KITTY_TOOL_CONTAINER_PIDS_LIMIT", "128")),
            tmpfs_size=os.getenv("KITTY_TOOL_CONTAINER_TMPFS_SIZE", "64m").strip() or "64m",
            extra_readonly_mounts=mounts,
        )


class ContainerToolExecutor:
    """Executes tools inside a short-lived Docker container sandbox."""

    def __init__(
        self,
        config: ContainerSandboxConfig,
        *,
        max_output_bytes: int = 65536,
    ):
        if not config.image:
            raise ValueError("container sandbox image is required")
        if max_output_bytes < 1024:
            raise ValueError("max_output_bytes must be at least 1024")
        if config.pids_limit < 1:
            raise ValueError("pids_limit must be positive")
        self.config = config
        self.max_output_bytes = max_output_bytes

    async def execute(
        self,
        spec: Any,
        arguments: Mapping[str, Any],
        timeout_seconds: float,
    ) -> ToolExecution:
        handler_ref = getattr(spec, "handler_ref", "") or infer_handler_ref(spec.handler)
        if not handler_ref:
            return ToolExecution(
                name=spec.name,
                ok=False,
                error="tool is not sandbox-capable; provide handler_ref='module:function'",
            )
        payload = json.dumps(
            {
                "name": spec.name,
                "handler_ref": handler_ref,
                "arguments": dict(arguments),
                "max_output_bytes": self.max_output_bytes,
            },
            ensure_ascii=False,
        ).encode("utf-8")
        process = await asyncio.create_subprocess_exec(
            *self.build_command(),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, stderr = await asyncio.wait_for(
                process.communicate(payload),
                timeout=timeout_seconds,
            )
        except TimeoutError:
            process.kill()
            await process.wait()
            return ToolExecution(name=spec.name, ok=False, error="tool execution timed out")
        return _decode_runner_result(spec.name, process.returncode, stdout, stderr, self.max_output_bytes)

    def build_command(self) -> tuple[str, ...]:
        command = [
            "docker",
            "run",
            "--rm",
            "-i",
            "--network",
            self.config.network,
            "--read-only",
            "--cap-drop",
            "ALL",
            "--security-opt",
            "no-new-privileges",
            "--pids-limit",
            str(self.config.pids_limit),
            "--cpus",
            self.config.cpus,
            "--memory",
            self.config.memory,
            "--tmpfs",
            f"/tmp:rw,noexec,nosuid,size={self.config.tmpfs_size}",
        ]
        if self.config.workspace_root:
            command.extend(
                [
                    "--mount",
                    f"type=bind,source={self.config.workspace_root},target=/workspace,readonly",
                    "-w",
                    "/workspace",
                    "-e",
                    "PYTHONPATH=/workspace",
                ]
            )
        for mount in self.config.extra_readonly_mounts:
            source, separator, target = mount.partition(":")
            if not separator or not source or not target:
                raise ValueError(
                    "KITTY_TOOL_CONTAINER_READONLY_MOUNTS entries must use source:target"
                )
            command.extend(
                [
                    "--mount",
                    f"type=bind,source={source},target={target},readonly",
                ]
            )
        command.extend(
            [
                self.config.image,
                self.config.python_executable,
                "-m",
                "kitty.tools.subprocess_runner",
            ]
        )
        return tuple(command)


def _decode_runner_result(
    name: str,
    returncode: int | None,
    stdout: bytes,
    stderr: bytes,
    max_output_bytes: int,
) -> ToolExecution:
    if returncode != 0:
        message = stderr.decode("utf-8", errors="replace").strip()
        return ToolExecution(
            name=name,
            ok=False,
            error=message or f"tool subprocess exited with code {returncode}",
        )
    if len(stdout) > max_output_bytes:
        return ToolExecution(
            name=name,
            ok=False,
            error=f"tool output exceeded {max_output_bytes} bytes",
        )
    try:
        result = json.loads(stdout.decode("utf-8"))
    except json.JSONDecodeError as exc:
        return ToolExecution(
            name=name,
            ok=False,
            error=f"tool subprocess returned invalid JSON: {exc}",
        )
    return ToolExecution(
        name=name,
        ok=bool(result.get("ok")),
        output=result.get("output"),
        error=str(result.get("error") or ""),
    )


def infer_handler_ref(handler: Any) -> str:
    module = getattr(handler, "__module__", "")
    qualname = getattr(handler, "__qualname__", "")
    if not module or not qualname:
        return ""
    if qualname == "<lambda>" or "<locals>" in qualname:
        return ""
    return f"{module}:{qualname}"


def _jsonable(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if is_dataclass(value):
        return _jsonable(asdict(value))
    if isinstance(value, Mapping):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_jsonable(item) for item in value]
    if hasattr(value, "value") and isinstance(value.value, (str, int, float, bool)):
        return value.value
    if hasattr(value, "__dict__"):
        return _jsonable(vars(value))
    return str(value)


def _csv_env(name: str) -> tuple[str, ...]:
    return tuple(item.strip() for item in os.getenv(name, "").split(",") if item.strip())
