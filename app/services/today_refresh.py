"""今天的 15 分钟刷新。

同时从千川页面拉多个账户的推商品计划，不打巨量开放平台。
全部成功后才替换总览缓存，并逐个写入账户计划 Redis。
没跑完时，用户继续看上一份已发布的数据。
"""

from __future__ import annotations

import asyncio
from datetime import date
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.log_setup import get_task_logger
from app.models.advertiser import QianchuanAdvertiser
from app.clients import qianchuan_page
from app.services import overview_service, product_refund, today_plan_cache, uni_plan_service, workbench_session
from app.services.advertiser_dashboard import load_roi2_accounts, take_roi2_total
from app.services.sync_retry import async_retry

logger = get_task_logger()
_refresh_lock = asyncio.Lock()


async def refresh_today(db: Session) -> dict[str, Any]:
    """拉齐今天全部账户。失败则不替换已发布的总览和计划。"""
    if _refresh_lock.locked():
        logger.info("今天的刷新还在跑，跳过这次")
        return {"skipped": True}
    async with _refresh_lock:
        return await _refresh_today(db)


async def _refresh_today(db: Session) -> dict[str, Any]:
    today = date.today().isoformat()
    settings = get_settings()
    concurrency = max(1, int(settings.TODAY_PLAN_CONCURRENCY or 1))
    adv_ids = [int(x) for x in db.scalars(select(QianchuanAdvertiser.advertiser_id)).all()]
    try:
        workbench = await async_retry(
            lambda: load_roi2_accounts(today, today),
            attempts=4,
            base_delay_seconds=3,
            label=f"今天刷新-工作台 {today}",
        )
    except Exception as exc:  # noqa: BLE001
        logger.exception("今天刷新：工作台没拉到，继续用上一份缓存：%s", exc)
        return {"published": False, "reason": "workbench"}
    official_total = take_roi2_total(today, today)
    try:
        cookie = workbench_session.require_session(db).cookie or ""
    except Exception as exc:  # noqa: BLE001
        logger.exception("今天刷新：工作台登录没拿到，不替换已发布数据：%s", exc)
        return {"published": False, "reason": "workbench_cookie"}

    plans_by, failed = await _load_plans_concurrent(cookie, adv_ids, today, concurrency)
    if failed is not None:
        return failed
    for adv_id, plans in plans_by.items():
        uni_plan_service.apply_local_commission(db, adv_id, plans)
        product_refund.attach_refund_rates(db, adv_id, plans)
    snapshot = overview_service.assemble_today_snapshot(
        db, today, workbench, plans_by, official_total=official_total
    )
    await overview_service.publish_today_snapshot(snapshot)
    for adv_id, plans in plans_by.items():
        await today_plan_cache.put_account(
            today,
            adv_id,
            plans,
            start_date=today,
            end_date=today,
        )
    logger.info("今天刷新已整份替换：账户 %s", len(adv_ids))
    return {"published": True, "advertisers": len(adv_ids)}


async def _load_plans_concurrent(
    cookie: str,
    adv_ids: list[int],
    today: str,
    concurrency: int,
) -> tuple[dict[int, list[dict[str, Any]]], dict[str, Any] | None]:
    """同时拉多个账户。任一账户失败就整份不发布，已在飞的请求自然结束。"""
    gate = asyncio.Semaphore(concurrency)
    plans_by: dict[int, list[dict[str, Any]]] = {}
    state: dict[str, Any] = {"done": 0, "failed": None}
    total = len(adv_ids)

    async def one(adv_id: int) -> None:
        if state["failed"] is not None:
            return
        async with gate:
            if state["failed"] is not None:
                return
            try:
                plans = await async_retry(
                    lambda adv_id=adv_id: qianchuan_page.fetch_product_plans(cookie, adv_id, today, today),
                    attempts=4,
                    base_delay_seconds=3,
                    label=f"今天刷新-计划 {adv_id}",
                )
            except Exception as exc:  # noqa: BLE001
                state["failed"] = {
                    "published": False,
                    "failed_advertiser": adv_id,
                    "done": state["done"],
                    "total": total,
                }
                logger.exception(
                    "今天刷新：账户 %s 没拉全（已完成 %s/%s），不替换已发布数据：%s",
                    adv_id,
                    state["done"],
                    total,
                    exc,
                )
                return
            plans_by[adv_id] = plans
            state["done"] += 1
            logger.info(
                "今天刷新：账户 %s 已拉齐 %s/%s，计划 %s 条，同时 %s 个",
                adv_id,
                state["done"],
                total,
                len(plans),
                concurrency,
            )

    await asyncio.gather(*(one(adv_id) for adv_id in adv_ids))
    return plans_by, state["failed"]
