"""定时同步用的异步重试（降速 + 多次尝试）。"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from typing import TypeVar

logger = logging.getLogger("uvicorn.error")

T = TypeVar("T")


async def async_retry(
    fn: Callable[[], Awaitable[T]],
    *,
    attempts: int = 5,
    base_delay_seconds: float = 3.0,
    label: str = "任务",
) -> T:
    """失败则指数退避重试，最后一次仍失败则抛出原异常。"""
    last: BaseException | None = None
    tries = max(1, int(attempts))
    for i in range(tries):
        try:
            return await fn()
        except Exception as exc:  # noqa: BLE001
            last = exc
            if i >= tries - 1:
                break
            wait = base_delay_seconds * (i + 1)
            logger.warning("%s 第 %s/%s 次失败，%.1f 秒后重试：%s", label, i + 1, tries, wait, exc)
            await asyncio.sleep(wait)
    assert last is not None
    raise last
