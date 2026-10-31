"""后台定时：刷新 token + 半小时预热今天/7天/30天仪表盘缓存。"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta

from app.core.config import get_settings
from app.core.database import SessionLocal
from app.services import dashboard_cache, token_store, workbench_session

logger = logging.getLogger("uvicorn.error")

REFRESH_AHEAD = timedelta(hours=2)


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
