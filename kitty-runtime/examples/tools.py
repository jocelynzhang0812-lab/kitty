from __future__ import annotations

from kitty.tools.registry import ToolRegistry


def register_tools(registry: ToolRegistry) -> None:
    """Register neutral example tools used by the production factory."""

    registry.add(
        "calculate_sum",
        lambda numbers: sum(numbers),
        description="Return the sum of a list of numbers.",
        parameters={
            "type": "object",
            "properties": {
                "numbers": {
                    "type": "array",
                    "items": {"type": "number"},
                    "description": "Numbers to add together.",
                }
            },
            "required": ["numbers"],
            "additionalProperties": False,
        },
    )
