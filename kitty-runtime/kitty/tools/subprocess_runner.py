from __future__ import annotations

import asyncio
import contextlib
import importlib
import inspect
import json
import sys
from collections.abc import Mapping
from typing import Any

from kitty.tools.executor import _jsonable


def main() -> int:
    try:
        request = json.loads(sys.stdin.buffer.read().decode("utf-8"))
        name = str(request["name"])
        handler_ref = str(request["handler_ref"])
        arguments = request.get("arguments") or {}
        max_output_bytes = int(request.get("max_output_bytes") or 65536)
        if not isinstance(arguments, Mapping):
            raise TypeError("arguments must be an object")
        handler = _resolve(handler_ref)
        with contextlib.redirect_stdout(sys.stderr):
            value = handler(**dict(arguments))
            if inspect.isawaitable(value):
                value = asyncio.run(value)
        return _write({"ok": True, "output": _jsonable(value), "error": ""}, max_output_bytes)
    except Exception as exc:
        name = locals().get("name", "tool")
        max_output_bytes = int(locals().get("max_output_bytes", 65536))
        return _write(
            {"ok": False, "output": None, "error": f"{type(exc).__name__}: {exc}"},
            max_output_bytes,
        )


def _resolve(handler_ref: str) -> Any:
    module_name, separator, attr_path = handler_ref.partition(":")
    if not separator or not module_name or not attr_path:
        raise ValueError("handler_ref must use module:function syntax")
    value: Any = importlib.import_module(module_name)
    for part in attr_path.split("."):
        value = getattr(value, part)
    if not callable(value):
        raise TypeError(f"{handler_ref} is not callable")
    return value


def _write(payload: dict[str, Any], max_output_bytes: int) -> int:
    encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    if len(encoded) > max_output_bytes:
        encoded = json.dumps(
            {
                "ok": False,
                "output": None,
                "error": f"tool output exceeded {max_output_bytes} bytes",
            },
            separators=(",", ":"),
        ).encode("utf-8")
    sys.stdout.buffer.write(encoded)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
