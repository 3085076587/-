"""每日同步：把某天的账户级 + 商品级数据入库。供 00:01 定时任务与手动脚本复用。

账户级复用工作台（load_roi2_accounts，不改子账户页）；商品级走开放平台 data/get，
同步时会自动用计划列表给当天活跃商品补名。
"""

from __future__ import annotations

import asyncio
from datetime import date, timedelta
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.log_setup import get_task_logger
from app.models.advertiser import QianchuanAdvertiser
from app.models.qianchuan_overview_daily import QianchuanOverviewDaily
from app.services import account_report_service, overview_daily_service, product_report_service, token_store
from app.services.advertiser_dashboard import load_roi2_accounts
from app.services.daily_sync_status import mark_finished, mark_running
from app.services.overview_daily_service import ALL_PERSON
from app.services.sync_retry import async_retry

logger = get_task_logger()

# 与总览利润起始日一致：更早的日期本来就不入库，不参与「是否汇总完成」拦截。
DATA_FROM = date(2026, 10, 1)


def yesterday_str() -> str:
    return (date.today() - timedelta(days=1)).isoformat()


def all_advertiser_ids(db: Session) -> list[int]:
    return [int(x) for x in db.scalars(select(QianchuanAdvertiser.advertiser_id)).all()]


def _iter_days(start: str, end: str) -> list[str]:
    d0 = date.fromisoformat(start)
    d1 = date.fromisoformat(end)
    days: list[str] = []
    while d0 <= d1:
        days.append(d0.isoformat())
        d0 += timedelta(days=1)
    return days


def verify_day_completeness(db: Session, day: str, advertiser_ids: list[int] | None = None) -> dict[str, Any]:
    """检查某天账户/总览/商品标记是否齐全（不打接口）。"""
    adv_ids = advertiser_ids if advertiser_ids is not None else all_advertiser_ids(db)
    stat = date.fromisoformat(day)
    overview_rows = db.scalar(
        select(func.count())
        .select_from(QianchuanOverviewDaily)
        .where(QianchuanOverviewDaily.stat_date == stat)
    )
    overview_all = db.scalar(
        select(func.count())
        .select_from(QianchuanOverviewDaily)
        .where(
            QianchuanOverviewDaily.stat_date == stat,
            QianchuanOverviewDaily.person == ALL_PERSON,
        )
    )
    missing_account = account_report_service.advertisers_missing_account_day(db, day, adv_ids)
    missing_product = product_report_service.advertisers_missing_product_marker(db, day, adv_ids)
    complete = (
        len(missing_account) == 0
        and int(overview_all or 0) > 0
        and len(missing_product) == 0
    )
    return {
        "day": day,
        "advertiser_total": len(adv_ids),
        "account_day_missing": missing_account,
        "account_day_missing_count": len(missing_account),
        "overview_row_count": int(overview_rows or 0),
        "overview_all_ok": int(overview_all or 0) > 0,
        "product_marker_missing": missing_product,
        "product_marker_missing_count": len(missing_product),
        "complete": complete,
    }


def incomplete_history_days(db: Session, start: str, end: str) -> list[str]:
    """区间内还没验收通过的历史日。今天单独查询返回空列表。"""
    today = date.today()
    yesterday = today - timedelta(days=1)
    hist_end = date.fromisoformat(end) if end < today.isoformat() else yesterday
    hist_start = max(date.fromisoformat(start), DATA_FROM)
    if hist_start > hist_end:
        return []
    bad: list[str] = []
    for day in _iter_days(hist_start.isoformat(), hist_end.isoformat()):
        if not verify_day_completeness(db, day)["complete"]:
            bad.append(day)
    return bad


def history_range_ready(db: Session, start: str, end: str) -> tuple[bool, str | None]:
    """区间内每个已结束、且不早于 DATA_FROM 的自然日是否都已验收通过。"""
    bad = incomplete_history_days(db, start, end)
    if not bad:
        return True, None
    shown = "、".join(bad[:8])
    extra = f" 等 {len(bad)} 天" if len(bad) > 8 else ""
    return False, f"{shown}{extra} 还没有入库，这几天先不计入。"


async def ensure_history_ready_for_overview(db: Session, start: str, end: str) -> list[str]:
    """缺的历史日不拦总览。返回还没入库的日期，页面用库里已有的商品继续算。"""
    return incomplete_history_days(db, start, end)


async def sync_day_all(db: Session, day: str) -> dict[str, Any]:
    """入库指定某天的账户 + 全部账户商品数据。返回统计。"""
    await mark_running(day)
    settings = get_settings()
    retries = max(1, int(settings.DAILY_SYNC_MAX_RETRIES or 5))
    base_delay = float(settings.DAILY_SYNC_RETRY_BASE_SECONDS or 3.0)
    pause = max(0.0, float(settings.DAILY_SYNC_PAUSE_SECONDS or 0))
    concurrency = max(1, int(settings.DAILY_SYNC_CONCURRENCY or 1))

    result: dict[str, Any] = {
        "day": day,
        "account_rows": 0,
        "overview_rows": 0,
        "product_ok": 0,
        "product_fail": 0,
        "product_retries": 0,
        "markers_filled": 0,
    }

    try:

        async def load_accounts() -> list[dict[str, Any]]:
            return await load_roi2_accounts(day, day)

        accounts: list[dict[str, Any]] = []
        try:
            accounts = await async_retry(
                load_accounts,
                attempts=retries,
                base_delay_seconds=base_delay,
                label=f"每日同步-工作台 {day}",
            )
        except Exception as exc:  # noqa: BLE001
            result["account_rows"] = -1
            result["overview_rows"] = -1
            logger.exception("每日同步-工作台失败 %s：%s", day, exc)
            accounts = []

        if accounts:
            try:
                result["account_rows"] = account_report_service.upsert_account_day_rows(db, day, accounts)
                adv_ids_early = all_advertiser_ids(db)
                result["account_zero_filled"] = account_report_service.fill_missing_account_days_zero(
                    db, day, adv_ids_early
                )
            except Exception as exc:  # noqa: BLE001
                result["account_rows"] = -1
                logger.exception("每日同步-账户日表失败 %s：%s", day, exc)
            try:
                result["overview_rows"] = overview_daily_service.save_overview_day(db, day, accounts)
            except Exception as exc:  # noqa: BLE001
                result["overview_rows"] = -1
                logger.exception("每日同步-总览汇总失败 %s：%s", day, exc)

        adv_ids = all_advertiser_ids(db)
        pre_markers = product_report_service.finalize_product_markers_for_day(db, day, adv_ids)
        need_pull = product_report_service.advertisers_missing_product_marker(db, day, adv_ids)
        logger.info(
            "每日同步 %s：商品预补标记 %s，仍需拉接口 %s / %s 户",
            day,
            pre_markers,
            len(need_pull),
            len(adv_ids),
        )

        client = await token_store.build_client(db)
        gate = asyncio.Semaphore(concurrency)
        extra_retries = 0

        async def sync_one(adv: int) -> bool:
            async with gate:
                if pause:
                    await asyncio.sleep(pause)

                async def pull() -> None:
                    await product_report_service.sync_day(client, db, adv, day)

                try:
                    await async_retry(
                        pull,
                        attempts=retries,
                        base_delay_seconds=base_delay,
                        label=f"每日同步-商品 账户{adv} {day}",
                    )
                    return True
                except Exception as exc:  # noqa: BLE001
                    logger.warning("每日同步-商品失败 账户%s %s：%s", adv, day, exc)
                    return False

        for adv in need_pull:
            await sync_one(adv)

        retry_round = product_report_service.advertisers_missing_product_marker(db, day, adv_ids)
        if retry_round:
            logger.info("每日同步 %s：商品标记仍缺 %s 户，开始第二轮重试", day, len(retry_round))
            for adv in retry_round:
                extra_retries += 1
                await asyncio.sleep(base_delay * 2)
                await sync_one(adv)

        post_markers = product_report_service.finalize_product_markers_for_day(db, day, adv_ids)
        result["markers_filled"] = int(pre_markers) + int(post_markers)
        still_missing = product_report_service.advertisers_missing_product_marker(db, day, adv_ids)
        ok = len(adv_ids) - len(still_missing)
        result.update(
            product_ok=ok,
            product_fail=len(still_missing),
            product_total=len(adv_ids),
            product_retries=extra_retries,
            product_still_missing=still_missing[:20],
            verify=verify_day_completeness(db, day, adv_ids),
        )
        logger.info(
            "每日同步完成 %s：账户 %s 行，总览汇总 %s 行，商品 成功 %s / 仍缺标记 %s / 共 %s，补标记 %s，完整=%s",
            day,
            result["account_rows"],
            result["overview_rows"],
            ok,
            len(still_missing),
            len(adv_ids),
            result["markers_filled"],
            result["verify"]["complete"],
        )
        return result
    finally:
        snapshot = result.get("verify") or verify_day_completeness(db, day)
        await mark_finished(day, complete=bool(snapshot.get("complete")), detail=snapshot)
