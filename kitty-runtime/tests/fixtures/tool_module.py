from __future__ import annotations

import asyncio
import time


def add(a: int, b: int) -> int:
    return a + b


def noisy(value: str) -> dict[str, str]:
    print("tool stdout should not corrupt the protocol")
    return {"value": value}


def sleep_for(seconds: float) -> str:
    time.sleep(seconds)
    return "done"


async def async_upper(value: str) -> str:
    await asyncio.sleep(0)
    return value.upper()
