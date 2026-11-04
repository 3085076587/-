"""今天的 15 分钟刷新。

按账户串行拉齐计划和商品。全部成功后才替换总览缓存，并逐个写入账户计划。
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
from app.services import overview_service, product_report_service, today_plan_cache, token_store, uni_plan_service
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
    pause = max(0.0, float(settings.TODAY_ACCOUNT_PAUSE_SECONDS or 0))
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

    client = await token_store.build_client(db)
    plans_by: dict[int, list[dict[str, Any]]] = {}
    products_by: dict[int, list[dict[str, Any]]] = {}
    for index, adv_id in enumerate(adv_ids, start=1):
        try:
            plans = await async_retry(
                lambda adv_id=adv_id: _load_plans(db, adv_id, today),
                attempts=4,
                base_delay_seconds=3,
                label=f"今天刷新-计划 {adv_id}",
            )
            products = await async_retry(
                lambda adv_id=adv_id: product_report_service.fetch_products(client, adv_id, today, today),
                attempts=4,
                base_delay_seconds=3,
                label=f"今天刷新-商品 {adv_id}",
            )
        except Exception as exc:  # noqa: BLE001
            logger.exception(
                "今天刷新：账户 %s 没拉全（%s/%s），不替换已发布数据：%s",
                adv_id,
                index,
                len(adv_ids),
                exc,
            )
            return {"published": False, "failed_advertiser": adv_id, "done": index - 1, "total": len(adv_ids)}
        plans_by[adv_id] = plans
        products_by[adv_id] = products
        logger.info("今天刷新：账户 %s 已拉齐 %s/%s，计划 %s 条", adv_id, index, len(adv_ids), len(plans))
        if pause and index < len(adv_ids):
            await asyncio.sleep(pause)

    snapshot = overview_service.assemble_today_snapshot(
        db, today, workbench, plans_by, products_by, official_total=official_total
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


async def _load_plans(db: Session, advertiser_id: int, today: str) -> list[dict[str, Any]]:
    data = await uni_plan_service.list_uni_plans(
        db,
        advertiser_id,
        start_date=today,
        end_date=today,
        sync_commission=True,
    )
    return list(data.get("plans") or [])
