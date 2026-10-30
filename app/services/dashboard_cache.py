"""子账户仪表盘：今天/7天/30天 Redis DB1 缓存。

参考 ERP 账户中心：预热与页面同区间只拉一次（单飞），后到的等待同一份结果。
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any, Awaitable, Callable

from sqlalchemy.orm import Session

from app.core.redis_client import get_cache_redis
from app.services import advertiser_dashboard

logger = logging.getLogger("uvicorn.error")

ProgressCb = Callable[[int, int, str], Awaitable[None]] | None

CACHE_PREFIX = "ad_helper:dashboard:v2"
PROGRESS_PREFIX = "ad_helper:dashboard:progress"
PRESET_RANGES = ("today", "7", "30")


@dataclass
class _Flight:
    force: bool
    done: asyncio.Future
    result: dict[str, Any] | None = None
    error: BaseException | None = None
    fresh: bool = False
    waiters: int = 0


_flights: dict[str, _Flight] = {}
_flight_lock = asyncio.Lock()
_warmup_lock = asyncio.Lock()


def range_dates(range_key: str) -> tuple[str, str]:
    end = date.today()
    start = end
    if range_key == "7":
        start = end - timedelta(days=6)
    elif range_key == "30":
        start = end - timedelta(days=29)
    return start.isoformat(), end.isoformat()


def cache_key(range_key: str, day: str | None = None) -> str:
    day = day or date.today().isoformat()
    return f"{CACHE_PREFIX}:{range_key}:{day}"


def progress_key(range_key: str) -> str:
    return f"{PROGRESS_PREFIX}:{range_key}"


async def set_progress(
    range_key: str,
    *,
    status: str,
    current: int = 0,
    total: int = 0,
    message: str = "",
) -> None:
    try:
        r = get_cache_redis()
        payload = {
            "status": status,
            "current": current,
            "total": total,
            "percent": int(current * 100 / total) if total else 0,
            "message": message,
        }
        await r.set(progress_key(range_key), json.dumps(payload, ensure_ascii=False), ex=3600)
    except Exception as exc:
        logger.warning("写仪表盘进度失败：%s", exc)


async def get_progress(range_key: str) -> dict[str, Any]:
    try:
        r = get_cache_redis()
        raw = await r.get(progress_key(range_key))
        if not raw:
            return {"status": "idle", "current": 0, "total": 0, "percent": 0, "message": ""}
        return json.loads(raw)
    except Exception:
        return {"status": "idle", "current": 0, "total": 0, "percent": 0, "message": ""}


async def get_cached(range_key: str) -> dict[str, Any] | None:
    if range_key not in PRESET_RANGES:
        return None
    try:
        r = get_cache_redis()
        raw = await r.get(cache_key(range_key))
        if not raw:
            return None
        data = json.loads(raw)
        accounts = data.get("accounts") or []
        if accounts and "plan_count" not in accounts[0]:
            return None
        data["from_cache"] = True
        data["range"] = range_key
        return data
    except Exception as exc:
        logger.warning("读仪表盘缓存失败：%s", exc)
        return None


async def delete_cached(range_key: str) -> None:
    try:
        r = get_cache_redis()
        await r.delete(cache_key(range_key))
    except Exception as exc:
        logger.warning("删仪表盘缓存失败：%s", exc)


async def save_cached(range_key: str, data: dict[str, Any]) -> None:
    try:
        r = get_cache_redis()
        payload = dict(data)
        payload["cached_at"] = datetime.now().isoformat(sep=" ", timespec="seconds")
        payload["range"] = range_key
        await r.set(
            cache_key(range_key),
            json.dumps(payload, ensure_ascii=False),
            ex=36 * 3600,
        )
    except Exception as exc:
        logger.warning("写仪表盘缓存失败：%s", exc)


async def _build_once(db: Session, range_key: str) -> dict[str, Any]:
    start, end = range_dates(range_key)
    await set_progress(range_key, status="running", current=0, total=0, message="开始拉取…")

    async def on_progress(current: int, total: int, msg: str) -> None:
        await set_progress(
            range_key,
            status="running",
            current=current,
            total=total,
            message=msg,
        )

    data = await advertiser_dashboard.build_dashboard(
        db,
        start_date=start,
        end_date=end,
        progress_cb=on_progress,
    )
    data["from_cache"] = False
    data["range"] = range_key
    await save_cached(range_key, data)
    await set_progress(
        range_key,
        status="done",
        current=data.get("total") or 0,
        total=data.get("total") or 0,
        message="完成",
    )
    return data


async def fetch_and_cache(
    db: Session,
    range_key: str,
    *,
    force: bool = False,
) -> dict[str, Any]:
    """有缓存直接用；刷新先删再拉。

    预热和页面同时打同一区间时只拉一次，后到的等这份结果（对齐 ERP 账户中心）。
    """
    if range_key not in PRESET_RANGES:
        raise ValueError(f"不支持的 range: {range_key}")

    while True:
        async with _flight_lock:
            current = _flights.get(range_key)
            if current is None or current.done.done():
                loop = asyncio.get_running_loop()
                current = _Flight(force=force, done=loop.create_future())
                _flights[range_key] = current
                leader = True
            else:
                current.waiters += 1
                leader = False
                flight = current

        if not leader:
            try:
                await flight.done
                if flight.error is not None:
                    raise flight.error
                # 自己要强制刷新，但赶上的那次没产出新数据 → 再开一轮
                if force and not flight.fresh:
                    continue
                if flight.result is not None:
                    return dict(flight.result)
                cached = await get_cached(range_key)
                if cached is not None:
                    return cached
                continue
            finally:
                async with _flight_lock:
                    flight.waiters = max(0, flight.waiters - 1)
                    if (
                        flight.waiters <= 0
                        and flight.done.done()
                        and _flights.get(range_key) is flight
                    ):
                        _flights.pop(range_key, None)

        try:
            if force:
                await delete_cached(range_key)
            else:
                cached = await get_cached(range_key)
                if cached is not None:
                    current.result = cached
                    current.fresh = False
                    return cached

            data = await _build_once(db, range_key)
            current.result = data
            current.fresh = True
            return data
        except BaseException as exc:
            current.error = exc
            await set_progress(
                range_key,
                status="error",
                current=0,
                total=0,
                message=str(exc)[:200],
            )
            raise
        finally:
            async with _flight_lock:
                if not current.done.done():
                    current.done.set_result(True)
                if current.waiters <= 0 and _flights.get(range_key) is current:
                    _flights.pop(range_key, None)


async def warmup_all_presets(db: Session) -> None:
    """半小时/启动预热；与页面共用单飞，叠跑时直接跳过。"""
    if _warmup_lock.locked():
        logger.info("仪表盘预热已在进行，跳过本轮")
        return
    async with _warmup_lock:
        for key in PRESET_RANGES:
            try:
                await fetch_and_cache(db, key, force=True)
                logger.info("仪表盘缓存已预热 range=%s", key)
            except Exception as exc:
                logger.exception("仪表盘预热失败 range=%s: %s", key, exc)
