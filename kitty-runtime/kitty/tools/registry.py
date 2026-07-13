from __future__ import annotations

from collections.abc import Awaitable, Callable, Iterable, Mapping
from dataclasses import dataclass
from typing import Any

from kitty.tools.executor import (
    InProcessToolExecutor,
    SubprocessToolExecutor,
    ToolExecution,
    ToolPolicy,
)


ToolHandler = Callable[..., Awaitable[Any] | Any]


@dataclass(slots=True)
class ToolSpec:
    name: str
    description: str
    parameters: dict[str, Any]
    handler: ToolHandler
    timeout_seconds: float | None = None
    executor: str | None = None
    handler_ref: str = ""

    def to_model_schema(self) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }


class ToolRegistry:
    """Runtime tool registry with policy, validation, and pluggable execution."""

    def __init__(
        self,
        *,
        default_timeout_seconds: float = 30.0,
        allowlist: Iterable[str] | None = None,
        denylist: Iterable[str] | None = None,
        default_executor: str = "in_process",
        policy: ToolPolicy | None = None,
        subprocess_max_output_bytes: int = 65536,
    ):
        if default_executor not in {"in_process", "subprocess"}:
            raise ValueError("default_executor must be in_process or subprocess")
        self.default_timeout_seconds = default_timeout_seconds
        self.allowlist = frozenset(allowlist) if allowlist is not None else None
        self.policy = policy or ToolPolicy(
            denylist=frozenset(denylist) if denylist is not None else frozenset()
        )
        self.default_executor = default_executor
        self._executors = {
            "in_process": InProcessToolExecutor(),
            "subprocess": SubprocessToolExecutor(max_output_bytes=subprocess_max_output_bytes),
        }
        self._tools: dict[str, ToolSpec] = {}

    def register(self, spec: ToolSpec) -> None:
        if spec.name in self._tools:
            raise ValueError(f"tool already registered: {spec.name}")
        self._tools[spec.name] = spec

    def add(
        self,
        name: str,
        handler: ToolHandler,
        *,
        description: str = "",
        parameters: dict[str, Any] | None = None,
        timeout_seconds: float | None = None,
        executor: str | None = None,
        handler_ref: str = "",
    ) -> ToolSpec:
        if executor is not None and executor not in self._executors:
            raise ValueError("executor must be in_process or subprocess")
        spec = ToolSpec(
            name=name,
            description=description,
            parameters=parameters or {"type": "object", "properties": {}},
            handler=handler,
            timeout_seconds=timeout_seconds,
            executor=executor,
            handler_ref=handler_ref,
        )
        self.register(spec)
        return spec

    def schemas(self) -> list[dict[str, Any]]:
        return [
            spec.to_model_schema()
            for name, spec in self._tools.items()
            if self.allowlist is None or name in self.allowlist
            if not self.policy.check(name)
        ]

    async def execute(self, name: str, arguments: Mapping[str, Any] | None = None) -> ToolExecution:
        if self.allowlist is not None and name not in self.allowlist:
            return ToolExecution(name=name, ok=False, error="tool is not allowed")
        policy_error = self.policy.check(name)
        if policy_error:
            return ToolExecution(name=name, ok=False, error=policy_error)
        spec = self._tools.get(name)
        if spec is None:
            return ToolExecution(name=name, ok=False, error="tool is not registered")
        args = dict(arguments or {})
        validation_error = self._validate(spec, args)
        if validation_error:
            return ToolExecution(name=name, ok=False, error=validation_error)
        timeout = spec.timeout_seconds or self.default_timeout_seconds
        executor_name = spec.executor or self.default_executor
        executor = self._executors.get(executor_name)
        if executor is None:
            return ToolExecution(name=name, ok=False, error=f"unknown tool executor: {executor_name}")
        return await executor.execute(spec, args, timeout)

    @staticmethod
    def _validate(spec: ToolSpec, arguments: Mapping[str, Any]) -> str:
        required = spec.parameters.get("required", [])
        missing = [name for name in required if name not in arguments]
        if missing:
            return "missing required arguments: " + ", ".join(missing)
        properties = spec.parameters.get("properties", {})
        if spec.parameters.get("additionalProperties") is False:
            unexpected = [name for name in arguments if name not in properties]
            if unexpected:
                return "unexpected arguments: " + ", ".join(unexpected)
        return ""
