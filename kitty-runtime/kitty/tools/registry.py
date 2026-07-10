from __future__ import annotations

import asyncio
import inspect
from collections.abc import Awaitable, Callable, Iterable, Mapping
from dataclasses import asdict, dataclass, is_dataclass
from typing import Any


ToolHandler = Callable[..., Awaitable[Any] | Any]


@dataclass(slots=True)
class ToolSpec:
    name: str
    description: str
    parameters: dict[str, Any]
    handler: ToolHandler
    timeout_seconds: float | None = None

    def to_model_schema(self) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }


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


class ToolRegistry:
    """Runtime tool registry with allow-listing and timeout isolation."""

    def __init__(
        self,
        *,
        default_timeout_seconds: float = 30.0,
        allowlist: Iterable[str] | None = None,
    ):
        self.default_timeout_seconds = default_timeout_seconds
        self.allowlist = frozenset(allowlist) if allowlist is not None else None
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
    ) -> ToolSpec:
        spec = ToolSpec(
            name=name,
            description=description,
            parameters=parameters or {"type": "object", "properties": {}},
            handler=handler,
            timeout_seconds=timeout_seconds,
        )
        self.register(spec)
        return spec

    def schemas(self) -> list[dict[str, Any]]:
        return [
            spec.to_model_schema()
            for name, spec in self._tools.items()
            if self.allowlist is None or name in self.allowlist
        ]

    async def execute(self, name: str, arguments: Mapping[str, Any] | None = None) -> ToolExecution:
        if self.allowlist is not None and name not in self.allowlist:
            return ToolExecution(name=name, ok=False, error="tool is not allowed")
        spec = self._tools.get(name)
        if spec is None:
            return ToolExecution(name=name, ok=False, error="tool is not registered")
        args = dict(arguments or {})
        validation_error = self._validate(spec, args)
        if validation_error:
            return ToolExecution(name=name, ok=False, error=validation_error)
        try:
            timeout = spec.timeout_seconds or self.default_timeout_seconds
            if inspect.iscoroutinefunction(spec.handler):
                value = await asyncio.wait_for(spec.handler(**args), timeout=timeout)
            else:
                value = await asyncio.wait_for(
                    asyncio.to_thread(spec.handler, **args),
                    timeout=timeout,
                )
                if inspect.isawaitable(value):
                    value = await asyncio.wait_for(value, timeout=timeout)
            return ToolExecution(name=name, ok=True, output=value)
        except TimeoutError:
            return ToolExecution(name=name, ok=False, error="tool execution timed out")
        except Exception as exc:
            return ToolExecution(name=name, ok=False, error=f"{type(exc).__name__}: {exc}")

    def import_csbot_registry(self, csbot_registry: Any) -> int:
        """Import tools from CS-bot's observable ``ToolRegistry`` contract."""

        imported = 0
        for name, info in csbot_registry.get_all_tools().items():
            if name in self._tools:
                continue
            instance = info.get("instance")
            handler = info.get("function")
            if not callable(handler):
                continue
            schema: dict[str, Any] = {}
            if instance is not None and hasattr(instance, "to_openai_schema"):
                raw_schema = instance.to_openai_schema()
                schema = raw_schema.get("function", {}).get("parameters", {})
            self.add(
                name,
                handler,
                description=str(info.get("description") or ""),
                parameters=schema or {"type": "object", "properties": {}},
            )
            imported += 1
        return imported

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
