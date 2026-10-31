"""子账户仪表盘：今天/7天/30天 Redis DB1 缓存。

参考 ERP 账户中心：预热与页面同区间只拉一次（单飞），后到的等待同一份结果。
"""

from __future__ import annotations

import asyncio
import json
import logging
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any, Awaitable, Callable

from sqlalchemy.orm import Session

from app.core.redis_client import get_cache_redis
from app.services import advertiser_dashboard, token_store

logger = logging.getLogger("uvicorn.error")

ProgressCb = Callable[[int, int, str], Awaitable[None]] | None

CACHE_PREFIX = "ad_helper:dashboard:v7"
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


def cache_key(range_key: str, person: str, day: str | None = None) -> str:
    day = day or date.today().isoformat()
    return f"{CACHE_PREFIX}:{person}:{range_key}:{day}"


def _flight_id(range_key: str, person: str) -> str:
    return f"{range_key}:{person}"


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


_progress_lock = asyncio.Lock()
_progress_open: dict[str, int] = {}
_progress_numbers: dict[str, tuple[int, int]] = {}


async def _progress_open_job(range_key: str, total: int, message: str) -> None:
    async with _progress_lock:
        current, acc = _progress_numbers.get(range_key, (0, 0))
        if _progress_open.get(range_key, 0) <= 0:
            current, acc = 0, 0
        _progress_open[range_key] = _progress_open.get(range_key, 0) + 1
        acc += total
        _progress_numbers[range_key] = (current, acc)
    await set_progress(range_key, status="running", current=current, total=acc, message=message)


async def _progress_tick(range_key: str, delta: int, message: str) -> None:
    if delta <= 0:
        return
    async with _progress_lock:
        current, total = _progress_numbers.get(range_key, (0, 0))
        current += delta
        _progress_numbers[range_key] = (current, total)
    await set_progress(range_key, status="running", current=current, total=total, message=message)


async def _progress_close_job(range_key: str, error: str | None = None) -> None:
    async with _progress_lock:
        left = max(0, _progress_open.get(range_key, 1) - 1)
        _progress_open[range_key] = left
        current, total = _progress_numbers.get(range_key, (0, 0))
        if left <= 0:
            current = total
            _progress_numbers[range_key] = (current, total)
    if left > 0:
        return
    if error:
        await set_progress(range_key, status="error", current=current, total=total, message=error)
        return
    await set_progress(range_key, status="done", current=total, total=total, message="完成")


@asynccontextmanager
async def _progress_batch(range_key: str):
    """整批刷新占住进度，避免一个人拉完就把进度标成完成。"""
    async with _progress_lock:
        fresh = _progress_open.get(range_key, 0) <= 0
        _progress_open[range_key] = _progress_open.get(range_key, 0) + 1
        if fresh:
            _progress_numbers[range_key] = (0, 0)
    try:
        yield
    finally:
        await _progress_close_job(range_key)


def merge_dashboards(parts: list[dict[str, Any]]) -> dict[str, Any]:
    accounts: list[dict[str, Any]] = []
    stale = 0
    start = end = ""
    cached_at = ""
    from_cache = bool(parts)
    for part in parts:
        accounts.extend(part.get("accounts") or [])
        stale += int(part.get("stale_count") or 0)
        start = part.get("start_date") or start
        end = part.get("end_date") or end
        if part.get("cached_at"):
            cached_at = max(cached_at, str(part["cached_at"]))
        if not part.get("from_cache"):
            from_cache = False
    accounts.sort(key=lambda item: float(item.get("stat_cost") or 0), reverse=True)
    sum_cost = sum(float(item.get("stat_cost") or 0) for item in accounts)
    sum_pay = sum(float(item.get("pay_amount") or 0) for item in accounts)
    sum_settle = sum(float(item.get("settle_amount") or 0) for item in accounts)
    return {
        "start_date": start,
        "end_date": end,
        "overview": {
            "stat_cost": round(sum_cost, 2),
            "pay_amount": round(sum_pay, 2),
            "pay_roi": round(sum_pay / sum_cost, 4) if sum_cost > 0 else None,
            "settle_amount": round(sum_settle, 2),
            "settle_roi": round(sum_settle / sum_cost, 4) if sum_cost > 0 else None,
        },
        "accounts": accounts,
        "total": len(accounts),
        "stale_count": stale,
        "from_cache": from_cache,
        "cached_at": cached_at or None,
    }


async def get_cached(range_key: str, person: str) -> dict[str, Any] | None:
    if range_key not in PRESET_RANGES or not person:
        return None
    try:
        r = get_cache_redis()
        raw = await r.get(cache_key(range_key, person))
        if not raw:
            return None
        data = json.loads(raw)
        accounts = data.get("accounts") or []
        if accounts and "plan_count" not in accounts[0]:
            return None
        data["from_cache"] = True
        data["range"] = range_key
        data["person"] = person
        return data
    except Exception as exc:
        logger.warning("读仪表盘缓存失败：%s", exc)
        return None


async def save_cached(range_key: str, person: str, data: dict[str, Any]) -> None:
    try:
        r = get_cache_redis()
        payload = dict(data)
        payload["cached_at"] = datetime.now().isoformat(sep=" ", timespec="seconds")
        payload["range"] = range_key
        payload["person"] = person
        await r.set(
            cache_key(range_key, person),
            json.dumps(payload, ensure_ascii=False),
            ex=36 * 3600,
        )
    except Exception as exc:
        logger.warning("写仪表盘缓存失败：%s", exc)


async def _build_once(
    db: Session,
    range_key: str,
    person: str,
    previous: dict[str, Any] | None = None,
    client: Any = None,
    gate: asyncio.Semaphore | None = None,
) -> dict[str, Any]:
    start, end = range_dates(range_key)
    seen = 0

    async def on_progress(current: int, total: int, msg: str) -> None:
        nonlocal seen
        delta = max(0, int(current) - seen)
        seen = int(current)
        await _progress_tick(range_key, delta, msg or person)

    data = await advertiser_dashboard.build_dashboard(
        db,
        start_date=start,
        end_date=end,
        progress_cb=on_progress,
        previous_accounts=(previous or {}).get("accounts"),
        person=person,
        client=client,
        gate=gate,
    )
    data["from_cache"] = False
    data["range"] = range_key
    data["person"] = person
    await save_cached(range_key, person, data)
    return data


async def fetch_and_cache(
    db: Session,
    range_key: str,
    person: str,
    *,
    force: bool = False,
    client: Any = None,
    gate: asyncio.Semaphore | None = None,
) -> dict[str, Any]:
    """同一个人、同一个时间范围只拉一次。后点的人等这一次，共用进度条。"""
    if range_key not in PRESET_RANGES:
        raise ValueError(f"不支持的 range: {range_key}")
    if not person:
        raise ValueError("缺少人名")

    flight_id = _flight_id(range_key, person)
    while True:
        async with _flight_lock:
            current = _flights.get(flight_id)
            if current is None or current.done.done():
                loop = asyncio.get_running_loop()
                current = _Flight(force=force, done=loop.create_future())
                _flights[flight_id] = current
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
                if force and not flight.fresh:
                    continue
                if flight.result is not None:
                    return dict(flight.result)
                cached = await get_cached(range_key, person)
                if cached is not None:
                    return cached
                continue
            finally:
                async with _flight_lock:
                    flight.waiters = max(0, flight.waiters - 1)
                    if (
                        flight.waiters <= 0
                        and flight.done.done()
                        and _flights.get(flight_id) is flight
                    ):
                        _flights.pop(flight_id, None)

        opened = False
        try:
            cached = await get_cached(range_key, person)
            if cached is not None and not force:
                current.result = cached
                current.fresh = False
                return cached

            total = advertiser_dashboard.count_person_accounts(db, person)
            await _progress_open_job(range_key, total, f"拉取 {person}")
            opened = True
            data = await _build_once(
                db,
                range_key,
                person,
                None,
                client=client,
                gate=gate,
            )
            current.result = data
            current.fresh = True
            return data
        except BaseException as exc:
            current.error = exc
            if opened:
                await _progress_close_job(range_key, str(exc)[:200])
                opened = False
            raise
        finally:
            if opened:
                await _progress_close_job(range_key)
            async with _flight_lock:
                if not current.done.done():
                    current.done.set_result(True)
                if current.waiters <= 0 and _flights.get(flight_id) is current:
                    _flights.pop(flight_id, None)


async def fetch_people(
    db: Session,
    range_key: str,
    people: list[str],
    *,
    force: bool = False,
) -> dict[str, Any]:
    parts: list[dict[str, Any]] = []
    gate = asyncio.Semaphore(2)
    client = await token_store.build_client(db) if people else None
    errors: list[str] = []

    async def one(person: str) -> dict[str, Any] | None:
        try:
            return await fetch_and_cache(
                db, range_key, person, force=force, client=client, gate=gate
            )
        except Exception as exc:
            logger.exception("拉取 %s 失败：%s", person, exc)
            if force:
                errors.append(f"{person}：{exc}")
                return None
            return await get_cached(range_key, person)

    async with _progress_batch(range_key):
        fetched = await asyncio.gather(*(one(person) for person in people))
    if errors:
        from app.core.exceptions import AppError

        raise AppError("有账户没有拉成功，请再点一次刷新。" + " ".join(errors))
    parts.extend(part for part in fetched if part)
    if not parts:
        start, end = range_dates(range_key)
        return merge_dashboards(
            [{"accounts": [], "start_date": start, "end_date": end, "from_cache": False, "stale_count": 0}]
        )
    return merge_dashboards(parts)


async def warmup_all_presets(db: Session) -> None:
    """按人名预热今天/7天/30天。某个人已经在拉时，预热等那一次，不重复请求。"""
    if _warmup_lock.locked():
        logger.info("仪表盘预热已在进行，跳过本轮")
        return
    async with _warmup_lock:
        people = advertiser_dashboard.list_people(db)
        gate = asyncio.Semaphore(2)
        client = await token_store.build_client(db) if people else None
        for key in PRESET_RANGES:
            async with _progress_batch(key):
                results = await asyncio.gather(
                    *(
                        fetch_and_cache(db, key, person, force=True, client=client, gate=gate)
                        for person in people
                    ),
                    return_exceptions=True,
                )
            for person, result in zip(people, results):
                if isinstance(result, Exception):
                    logger.error("仪表盘预热失败 person=%s range=%s: %s", person, key, result)
            logger.info("仪表盘缓存已预热 range=%s people=%s", key, len(people))
