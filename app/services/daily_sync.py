"""每日同步：把某天的账户级 + 商品级数据入库。供 00:01 定时任务与手动脚本复用。

账户级复用工作台（load_roi2_accounts，不改子账户页）；商品级走开放平台 data/get，
同步时会自动用计划列表给当天活跃商品补名。
"""

from __future__ import annotations

import asyncio
import logging
from datetime import date, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.models.advertiser import QianchuanAdvertiser
from app.services import account_report_service, product_report_service, token_store

logger = logging.getLogger("uvicorn.error")


def yesterday_str() -> str:
    return (date.today() - timedelta(days=1)).isoformat()


def all_advertiser_ids(db: Session) -> list[int]:
    return [int(x) for x in db.scalars(select(QianchuanAdvertiser.advertiser_id)).all()]


async def sync_day_all(db: Session, day: str) -> dict[str, Any]:
    """入库指定某天的账户 + 全部账户商品数据。返回统计。"""
    settings = get_settings()
    result: dict[str, Any] = {"day": day, "account_rows": 0, "product_ok": 0, "product_fail": 0}

    # 1) 账户维度：一次工作台请求写全部账户
    try:
        result["account_rows"] = await account_report_service.sync_account_day(db, day)
    except Exception as exc:  # noqa: BLE001
        result["account_rows"] = -1
        logger.exception("每日同步-账户失败 %s：%s", day, exc)

    # 2) 商品维度：逐账户单日入库，并发受限流保护
    client = await token_store.build_client(db)
    gate = asyncio.Semaphore(max(1, int(settings.DASHBOARD_CONCURRENCY or 2)))
    adv_ids = all_advertiser_ids(db)
    ok = fail = 0

    async def one(adv: int) -> None:
        nonlocal ok, fail
        async with gate:
            try:
                await product_report_service.sync_day(client, db, adv, day)
                ok += 1
            except Exception as exc:  # noqa: BLE001
                fail += 1
                logger.warning("每日同步-商品失败 账户%s %s：%s", adv, day, exc)

    await asyncio.gather(*(one(a) for a in adv_ids))
    result.update(product_ok=ok, product_fail=fail, product_total=len(adv_ids))
    logger.info(
        "每日同步完成 %s：账户 %s 行，商品 成功 %s / 失败 %s / 共 %s",
        day,
        result["account_rows"],
        ok,
        fail,
        len(adv_ids),
    )
    return result
