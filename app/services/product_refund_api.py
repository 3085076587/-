"""商品实际退款率。每次直接请求接口，不走 Redis。不做定时任务。"""

from __future__ import annotations

import logging

import httpx

from app.core.exceptions import AppError

logger = logging.getLogger("uvicorn.error")

REFUND_RATE_URL = "http://120.76.58.132:8082/api/v1/product-refund-rates"


async def load_rates() -> list[dict]:
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
