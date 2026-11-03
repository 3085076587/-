"""后台定时：刷新 token + 半小时预热今天/7天/30天仪表盘缓存。"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta

from app.core.config import get_settings
from app.core.database import SessionLocal
from app.core.log_setup import get_task_logger
from app.services import daily_sync, dashboard_cache, overview_service, token_store, workbench_session

logger = get_task_logger()

REFRESH_AHEAD = timedelta(hours=2)
DAILY_SYNC_HOUR = 0
DAILY_SYNC_MINUTE = 1
OVERVIEW_TODAY_SECONDS = 15 * 60


async def refresh_token_if_needed() -> None:
    db = SessionLocal()
    try:
        row = token_store.get_token_row(db)
        if row is None:
            logger.info("定时刷新：尚未保存 token，跳过")
            return
        now = datetime.now()
        if row.refresh_expires_at <= now:
            logger.error("定时刷新：refresh_token 已过期，需要重新授权")
            return
        if row.expires_at > now + REFRESH_AHEAD:
            logger.info(
                "定时刷新：access_token 仍有效，过期时间 %s，跳过",
                row.expires_at,
            )
            return
        refreshed = await token_store.refresh_access_token(db, row)
        logger.info("定时刷新成功，新过期时间 %s", refreshed.expires_at)
    except Exception as exc:
        logger.exception("定时刷新失败：%s", exc)
    finally:
        db.close()


async def refresh_workbench_if_needed() -> None:
    db = SessionLocal()
    try:
        await workbench_session.keepalive(db)
    except Exception as exc:
        logger.exception("工作台登录保活失败：%s", exc)
    finally:
        db.close()


async def warmup_dashboard_cache() -> None:
    db = SessionLocal()
    try:
        await dashboard_cache.warmup_all_presets(db)
    except Exception as exc:
        logger.exception("仪表盘缓存预热失败：%s", exc)
    finally:
        db.close()


async def refresh_overview_today(*, force: bool) -> None:
    """刷新今天总览缓存。force 为假时缓存还在就直接用，不打巨量。"""
    db = SessionLocal()
    try:
        data = await overview_service.load_today_snapshot(db, force=force)
        if data.get("from_cache") and not force:
            logger.info("今天的数据总览仍用缓存")
        else:
            logger.info("今天的数据总览已写入缓存，%s 秒后更新", OVERVIEW_TODAY_SECONDS)
    except Exception as exc:
        logger.exception("今天的数据总览刷新失败：%s", exc)
    finally:
        db.close()


async def overview_today_loop(stop_event: asyncio.Event) -> None:
    try:
        await asyncio.wait_for(stop_event.wait(), timeout=20)
        return
    except asyncio.TimeoutError:
        pass
    # 刚启动时缓存还有效就接着用，避免每次重启都把全部账户再打一遍。
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
    """同步昨天的账户 + 总览日汇总 + 商品数据入库（商品同步会顺带补当天活跃商品名）。"""
    db = SessionLocal()
    try:
        day = daily_sync.yesterday_str()
        logger.info("每日同步任务启动：目标日期 %s", day)
        await daily_sync.sync_day_all(db, day)
    except Exception as exc:  # noqa: BLE001
        logger.exception("每日同步任务异常：%s", exc)
    finally:
        db.close()


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
