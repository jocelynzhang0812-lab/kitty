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

        if process.returncode != 0:
            message = stderr.decode("utf-8", errors="replace").strip()
            return ToolExecution(
                name=spec.name,
                ok=False,
                error=message or f"tool subprocess exited with code {process.returncode}",
            )
        if len(stdout) > self.max_output_bytes:
            return ToolExecution(
                name=spec.name,
                ok=False,
                error=f"tool output exceeded {self.max_output_bytes} bytes",
            )
        try:
            result = json.loads(stdout.decode("utf-8"))
        except json.JSONDecodeError as exc:
            return ToolExecution(
                name=spec.name,
                ok=False,
                error=f"tool subprocess returned invalid JSON: {exc}",
            )
        return ToolExecution(
            name=spec.name,
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
