from __future__ import annotations

import hashlib
import importlib.util
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType

from kitty.hooks.bus import HookBinding, HookBus, HookCallback


@dataclass(slots=True, frozen=True)
class LoadedHook:
    path: Path
    module: ModuleType
    binding: HookBinding


def _module_name(path: Path) -> str:
    digest = hashlib.sha256(str(path).encode("utf-8")).hexdigest()[:12]
    return f"kitty_dynamic_hook_{digest}"


def load_hook(
    path: str | Path,
    bus: HookBus,
    *,
    timeout_seconds: float | None = None,
) -> LoadedHook:
    """Load a Python hook exposing ``hook(event, ctx)`` and event filters."""

    hook_path = Path(path).expanduser().resolve()
    if not hook_path.is_file():
        raise FileNotFoundError(f"hook file not found: {hook_path}")

    spec = importlib.util.spec_from_file_location(_module_name(hook_path), hook_path)
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot load hook module: {hook_path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    callback = getattr(module, "hook", None)
    if not callable(callback):
        raise TypeError(f"hook module must export callable 'hook': {hook_path}")
    callback = callback  # type: HookCallback

    listened = getattr(module, "listened_events", None)
    if listened is None:
        listened = getattr(callback, "listened_events", ())
    binding = bus.register(
        callback,
        listened_events=listened,
        name=f"{hook_path.stem}:{hook_path}",
        timeout_seconds=timeout_seconds,
    )
    return LoadedHook(path=hook_path, module=module, binding=binding)
