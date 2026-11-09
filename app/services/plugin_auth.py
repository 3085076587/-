"""插件登录只能改佣金和实际退款率，并且计划必须在今天的缓存里。"""

from __future__ import annotations

import time
from datetime import date

from fastapi import Request

from app.core.exceptions import AppError
from app.services import today_plan_cache
from app.services.account_access import current_username

PLUGIN_WRITE_PATHS = {
    "/api/plugin/profit-input",
    "/api/plan/commission",
    "/api/product/refund-rate",
}
_LIMIT = 30
_WINDOW_SECONDS = 60
_hits: dict[str, list[float]] = {}


def is_plugin(request: Request) -> bool:
    return getattr(request.state, "credential", "") == "plugin"


def _rate_limit(username: str) -> None:
    now = time.time()
    recent = [stamp for stamp in _hits.get(username, []) if now - stamp < _WINDOW_SECONDS]
    if len(recent) >= _LIMIT:
        raise AppError("写入太频繁，请稍后再试", code=42901)
    recent.append(now)
    _hits[username] = recent


async def guard_plugin_write(
    request: Request,
    advertiser_id: int,
    *,
    ad_id: int | None = None,
    product_id: int | None = None,
) -> dict | None:
    """网页登录直接返回。插件登录先限流，再确认这条计划或商品在今天的缓存里。"""
    if not is_plugin(request):
        return None
    _rate_limit(current_username(request))
    cached = await today_plan_cache.get_account(date.today().isoformat(), int(advertiser_id))
    plans = list((cached or {}).get("plans") or [])
    if ad_id is not None:
        matched = next((item for item in plans if int(item.get("ad_id") or 0) == int(ad_id)), None)
        if matched is None:
            raise AppError("今天的计划里没有这条计划", code=40302)
        return matched
    if product_id is not None:
        matched = next(
            (item for item in plans if int(item.get("product_id") or 0) == int(product_id)),
            None,
        )
        if matched is None:
            raise AppError("今天的计划里没有这个商品", code=40302)
        return matched
    raise AppError("缺少计划", code=40302)
