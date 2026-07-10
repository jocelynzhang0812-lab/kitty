import asyncio
import os
from abc import ABC, abstractmethod
from typing import Any, Dict, List, Optional, Callable
from dataclasses import dataclass
from enum import Enum
import time


class ToolStatus(Enum):
    SUCCESS = "success"
    FAILED = "failed"


@dataclass
class ToolResult:
    tool_name: str
    status: ToolStatus
    result: Any
    error_message: Optional[str] = None
    execution_time: float = 0.0


class ToolRegistry:
    _tools: Dict[str, Dict[str, Any]] = {}

    @classmethod
    def register(cls, name: str, func: Callable, instance_or_desc: Any):
        if isinstance(instance_or_desc, str):
            desc, instance = instance_or_desc, None
        else:
            desc = getattr(instance_or_desc, "description", "")
            instance = instance_or_desc
        cls._tools[name] = {
            "function": func,
            "description": desc,
            "name": name,
            "instance": instance,
        }

    @classmethod
    def get_tool(cls, name: str) -> Optional[Any]:
        return cls._tools.get(name, {}).get("function")

    @classmethod
    def get_instance(cls, name: str) -> Optional[Any]:
        return cls._tools.get(name, {}).get("instance")

    @classmethod
    def get_all_tools(cls) -> Dict[str, Dict[str, Any]]:
        return cls._tools.copy()

    @classmethod
    def list_tools(cls) -> List[str]:
        return list(cls._tools.keys())

    @classmethod
    async def execute_tool(cls, name: str, **kwargs) -> ToolResult:
        tool_func = cls.get_tool(name)
        if not tool_func:
            return ToolResult(
                tool_name=name,
                status=ToolStatus.FAILED,
                result=None,
                error_message=f"Tool '{name}' not found",
                execution_time=0.0,
            )
        try:
            start = time.time()
            timeout = float(os.getenv("CS_TOOL_TIMEOUT_SECONDS", "30"))
            result = await asyncio.wait_for(tool_func(**kwargs), timeout=timeout)
            result.execution_time = time.time() - start
            return result
        except asyncio.TimeoutError:
            return ToolResult(
                tool_name=name,
                status=ToolStatus.FAILED,
                result=None,
                error_message=f"tool timed out after {timeout:g}s",
                execution_time=time.time() - start,
            )
        except Exception as e:
            return ToolResult(
                tool_name=name,
                status=ToolStatus.FAILED,
                result=None,
                error_message=str(e),
                execution_time=0.0,
            )


class BaseTool(ABC):
    def __init__(self, name: str, description: str):
        self.name = name
        self.description = description
        async def _wrapper(**kwargs):
            return await self.execute(**kwargs)
        ToolRegistry.register(name, _wrapper, self)

    @abstractmethod
    async def execute(self, **kwargs) -> ToolResult:
        pass

    def get_schema(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "parameters": self._get_parameters(),
        }

    def to_openai_schema(self) -> Dict[str, Any]:
        """导出为 OpenAI Function Calling Schema（Kimi API 兼容）"""
        params = self._get_parameters()
        properties = {}
        required = []
        for k, v in params.items():
            prop = {pk: pv for pk, pv in v.items() if pk != "optional"}
            properties[k] = prop
            if not v.get("optional"):
                required.append(k)
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": {
                    "type": "object",
                    "properties": properties,
                    "required": required,
                },
            },
        }

    @abstractmethod
    def _get_parameters(self) -> Dict[str, Any]:
        pass
