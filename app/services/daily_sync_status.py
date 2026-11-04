"""每日入库进度：Redis 标记「正在跑 / 某天是否验收通过」。"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any

from app.core.redis_client import get_cache_redis

RUNNING_KEY = "ad_helper:daily_sync:running"
COMPLETE_PREFIX = "ad_helper:daily_sync:complete:"
RUNNING_TTL = 6 * 3600
COMPLETE_TTL = 45 * 24 * 3600


async def mark_running(day: str) -> None:
    r = get_cache_redis()
    payload = {"day": day, "started_at": datetime.now().isoformat(sep=" ", timespec="seconds")}
    await r.set(RUNNING_KEY, json.dumps(payload, ensure_ascii=False), ex=RUNNING_TTL)


async def mark_finished(day: str, *, complete: bool, detail: dict[str, Any] | None = None) -> None:
    r = get_cache_redis()
    await r.delete(RUNNING_KEY)
    body = {
        "day": day,
        "complete": bool(complete),
        "finished_at": datetime.now().isoformat(sep=" ", timespec="seconds"),
        "detail": detail or {},
    }
    await r.set(
        f"{COMPLETE_PREFIX}{day}",
        json.dumps(body, ensure_ascii=False),
        ex=COMPLETE_TTL,
    )


async def running_info() -> dict[str, Any] | None:
    r = get_cache_redis()
    raw = await r.get(RUNNING_KEY)
    if not raw:
        return None
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return {"day": str(raw)}


async def day_complete_cached(day: str) -> bool | None:
    """Redis 里该日是否已验收通过；无记录返回 None。"""
    r = get_cache_redis()
    raw = await r.get(f"{COMPLETE_PREFIX}{day}")
    if not raw:
        return None
    try:
        data = json.loads(raw)
        return bool(data.get("complete"))
    except json.JSONDecodeError:
        return None
