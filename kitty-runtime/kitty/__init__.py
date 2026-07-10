"""Clean-room Kitty-compatible agent runtime.

The package implements only the contracts observable from the CS-bot
repository.  It is not the original Kitty implementation.
"""

from kitty.core.config import KittyConfig
from kitty.core.context import AgentRecord, HookContext, RecordMeta
from kitty.core.events import EventType, SessionEvent, WireType
from kitty.runtime import KittyRuntime

__all__ = [
    "AgentRecord",
    "EventType",
    "HookContext",
    "KittyConfig",
    "KittyRuntime",
    "RecordMeta",
    "SessionEvent",
    "WireType",
]

__version__ = "0.1.0"
