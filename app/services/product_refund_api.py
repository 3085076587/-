"""商品实际退款率。页面来要时先读 Redis，没有再请求接口，写入后 5 小时过期。不做定时任务。"""

from __future__ import annotations

import json
import logging

import httpx

from app.core.exceptions import AppError
from app.core.redis_client import get_cache_redis

logger = logging.getLogger("uvicorn.error")

REFUND_RATE_URL = "http://120.76.58.132:8082/api/v1/product-refund-rates"
CACHE_KEY = "product_refund_rates:v1"
CACHE_TTL_SECONDS = 5 * 60 * 60


async def load_rates() -> list[dict]:
    cached = await _read_cache()
    if cached is not None:
        return cached
    rows = await _fetch()
    await get_cache_redis().set(
        CACHE_KEY,
        json.dumps(rows, ensure_ascii=False),
        ex=CACHE_TTL_SECONDS,
    )
    return rows


async def _read_cache() -> list[dict] | None:
    raw = await get_cache_redis().get(CACHE_KEY)
    if not raw:
        return None
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return None
    if not isinstance(data, list):
        return None
    return data


async def _fetch() -> list[dict]:
    try:
        async with httpx.AsyncClient(timeout=20.0) as client:
            response = await client.get(REFUND_RATE_URL)
            response.raise_for_status()
            data = response.json()
    except Exception as exc:  # noqa: BLE001
        logger.warning("商品退款率接口失败：%s", exc)
        raise AppError("商品退款率接口暂时拿不到") from exc
    if not isinstance(data, list):
        raise AppError("商品退款率接口返回的不是列表")
    return data
