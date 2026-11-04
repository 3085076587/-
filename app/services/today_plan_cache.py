"""今天每个子账户的计划缓存。一个账户一个键，改一个账户不会碰到其他账户。"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any

from app.core.redis_client import get_cache_redis

PREFIX = "ad_helper:plans:today:v1"
TTL_SECONDS = 36 * 3600


def _key(day: str, advertiser_id: int) -> str:
    return f"{PREFIX}:{day}:{int(advertiser_id)}"


async def get_account(day: str, advertiser_id: int) -> dict[str, Any] | None:
    raw = await get_cache_redis().get(_key(day, advertiser_id))
    if not raw:
        return None
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return None
    if not isinstance(data, dict):
        return None
    data["from_cache"] = True
    return data


async def put_account(
    day: str,
    advertiser_id: int,
    plans: list[dict[str, Any]],
    *,
    start_date: str,
    end_date: str,
) -> dict[str, Any]:
    """只写这一个账户。"""
    payload = {
        "advertiser_id": int(advertiser_id),
        "start_date": start_date,
        "end_date": end_date,
        "plans": plans,
        "total": len(plans),
        "cached_at": datetime.now().isoformat(sep=" ", timespec="seconds"),
        "from_cache": False,
    }
    await get_cache_redis().set(
        _key(day, advertiser_id),
        json.dumps(payload, ensure_ascii=False),
        ex=TTL_SECONDS,
    )
    return payload
