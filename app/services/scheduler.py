"""后台定时：刷新 token + 半小时预热今天/7天/30天仪表盘缓存。"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta

from app.core.config import get_settings
from app.core.database import SessionLocal
from app.core.exceptions import OceanEngineApiError
from app.core.log_setup import get_task_logger
from app.services import (
    account_scope,
    daily_sync,
    dashboard_cache,
    overview_service,
    plan_commission_sync,
    token_store,
    workbench_session,
)

logger = get_task_logger()

REFRESH_AHEAD = timedelta(hours=2)
DAILY_SYNC_HOUR = 0
DAILY_SYNC_MINUTE = 1
OVERVIEW_TODAY_SECONDS = 15 * 60


async def refresh_token_if_needed() -> None:
    db = SessionLocal()
    try:
        rows = token_store.list_token_rows(db)
        if not rows:
            logger.info("定时刷新：尚未保存 token，跳过")
            return
        seen: set[str] = set()
        now = datetime.now()
        for row in rows:
            if row.refresh_token in seen:
                continue
            seen.add(row.refresh_token)
            if row.refresh_expires_at <= now:
                logger.error("定时刷新：%s 的 refresh_token 已过期，需要重新授权", row.agent_id)
                continue
            if row.expires_at > now + REFRESH_AHEAD:
                logger.info("定时刷新：%s 的 access_token 仍有效，过期时间 %s", row.agent_id, row.expires_at)
                continue
            try:
                refreshed = await token_store.refresh_access_token(db, row)
            except OceanEngineApiError as exc:
                logger.error("定时刷新：账号 %s 需要重新授权：%s", row.agent_id, exc.msg)
                continue
            logger.info("定时刷新成功 agent_id=%s，新过期时间 %s", refreshed.agent_id, refreshed.expires_at)
    except Exception as exc:
        logger.exception("定时刷新失败：%s", exc)
    finally:
        db.close()


async def _each_account(work):
    db = SessionLocal()
    try:
        rows = [row for row in token_store.list_token_rows(db) if row.agent_id]
        if not rows:
            await work(db)
            return
        for row in rows:
            with account_scope.activated(row):
                try:
                    await work(db)
                except Exception as exc:  # noqa: BLE001
                    logger.exception("账号 %s 的任务失败：%s", row.agent_id, exc)
    finally:
        db.close()


async def refresh_workbench_if_needed() -> None:
    db = SessionLocal()
    try:
        await workbench_session.keepalive_all(db)
    except Exception as exc:
        logger.exception("工作台登录保活失败：%s", exc)
    finally:
        db.close()


async def warmup_dashboard_cache() -> None:
    async def _warm(db):
        await dashboard_cache.warmup_all_presets(db)

    try:
        await _each_account(_warm)
    except Exception as exc:
        logger.exception("仪表盘缓存预热失败：%s", exc)


async def refresh_overview_today(*, force: bool) -> None:
    """后台拉齐今天的全部账户。没完成不替换缓存，用户继续看上一份。"""
    del force
    from app.services import today_refresh

    async def _refresh(db):
        result = await today_refresh.refresh_today(db)
        if result.get("published"):
            logger.info(
                "账号 %s 今天的数据已整份替换，%s 秒后再次刷新",
                account_scope.agent_id(),
                OVERVIEW_TODAY_SECONDS,
            )
        else:
            logger.info("账号 %s 今天的刷新未完成，用户仍使用上一份数据：%s", account_scope.agent_id(), result)

    try:
        await _each_account(_refresh)
    except Exception as exc:
        logger.exception("今天的数据总览刷新失败：%s", exc)


async def refresh_plan_commissions() -> None:
    """每个授权账号各自拉计划列表，按名称佣金与快照对比后更新 plan_commission。"""

    async def _sync(db):
        stats = await plan_commission_sync.sync_all_advertisers(db)
        logger.info(
            "计划佣金同步完成 agent_id=%s：账户 %s，更新 %s，删除 %s，失败 %s",
            account_scope.agent_id(),
            stats.get("advertisers"),
            stats.get("updated"),
            stats.get("deleted"),
            stats.get("failed"),
        )

    try:
        await _each_account(_sync)
    except Exception as exc:  # noqa: BLE001
        logger.exception("计划佣金同步异常：%s", exc)


async def plan_commission_loop(stop_event: asyncio.Event) -> None:
    try:
        await asyncio.wait_for(stop_event.wait(), timeout=20)
        return
    except asyncio.TimeoutError:
        pass
    await refresh_plan_commissions()
    while not stop_event.is_set():
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=OVERVIEW_TODAY_SECONDS)
            break
        except asyncio.TimeoutError:
            pass
        await refresh_plan_commissions()


async def overview_today_loop(stop_event: asyncio.Event) -> None:
    try:
        await asyncio.wait_for(stop_event.wait(), timeout=20)
        return
    except asyncio.TimeoutError:
        pass
    # 今天的 15 分钟缓存还在就接着用。只有这份缓存是空的，启动时才跑一遍。
    db = SessionLocal()
    try:
        rows = [row for row in token_store.list_token_rows(db) if row.agent_id]
    finally:
        db.close()
    missing = False
    for row in rows:
        with account_scope.activated(row):
            if not await overview_service.today_cache_ready():
                missing = True
                logger.info("账号 %s 今天的 15 分钟缓存是空的", row.agent_id)
    if rows and not missing:
        logger.info("今天的 15 分钟缓存还在，启动时不重跑")
    else:
        logger.info("今天的 15 分钟缓存是空的，启动时开始拉取")
        await refresh_overview_today(force=False)
    while not stop_event.is_set():
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=OVERVIEW_TODAY_SECONDS)
            break
        except asyncio.TimeoutError:
            pass
        await refresh_overview_today(force=True)


async def token_refresh_loop(stop_event: asyncio.Event) -> None:
    """启动后循环：token 检查 + 预设区间缓存预热。"""
    settings = get_settings()
    interval = max(60, int(settings.DASHBOARD_CACHE_INTERVAL_SECONDS or 1800))

    try:
        await asyncio.wait_for(stop_event.wait(), timeout=20)
        return
    except asyncio.TimeoutError:
        pass

    # 启动后先保活工作台登录，再预热一轮
    await refresh_token_if_needed()
    await refresh_workbench_if_needed()
    await warmup_dashboard_cache()

    while not stop_event.is_set():
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=interval)
            break
        except asyncio.TimeoutError:
            pass
        await refresh_token_if_needed()
        await warmup_dashboard_cache()


def _seconds_until(hh: int, mm: int) -> float:
    now = datetime.now()
    target = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
    if target <= now:
        target += timedelta(days=1)
    return (target - now).total_seconds()


async def run_daily_sync() -> None:
    """每个授权账号各自同步昨天的账户、总览和商品。一个账号失败不影响另一个。"""

    async def _sync(db):
        day = daily_sync.yesterday_str()
        logger.info("每日同步任务启动：账号 %s，目标日期 %s", account_scope.agent_id(), day)
        result = await daily_sync.sync_day_all(db, day)
        verify = result.get("verify") or {}
        if not verify.get("complete"):
            logger.warning(
                "每日同步 %s 账号 %s 未完全入库：缺商品标记 %s 户，总览行 %s",
                day,
                account_scope.agent_id(),
                verify.get("product_marker_missing_count"),
                verify.get("overview_row_count"),
            )

    try:
        await _each_account(_sync)
    except Exception as exc:  # noqa: BLE001
        logger.exception("每日同步任务异常：%s", exc)


async def daily_sync_loop(stop_event: asyncio.Event) -> None:
    """每天 00:01 触发一次昨日数据入库。"""
    while not stop_event.is_set():
        wait = _seconds_until(DAILY_SYNC_HOUR, DAILY_SYNC_MINUTE)
        logger.info("每日同步：距下次 %02d:%02d 还有 %.0f 秒", DAILY_SYNC_HOUR, DAILY_SYNC_MINUTE, wait)
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=wait)
            return
        except asyncio.TimeoutError:
            pass
        await run_daily_sync()
