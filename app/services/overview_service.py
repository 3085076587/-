"""数据总览：消耗来自工作台账户表，利润与产品来自开放平台商品维度报表。

商品数据优先读库（qianchuan_product_daily 覆盖整个区间时零接口调用），
未覆盖再打商品接口（单账户一次请求）；聚合结果按人 + 日期区间缓存 Redis DB1。
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections import defaultdict
from datetime import date, datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.exceptions import AppError
from app.core.redis_client import get_cache_redis
from app.models.advertiser import QianchuanAdvertiser
from app.services import (
    account_report_service,
    product_report_service,
    token_store,
)
from app.services.advertiser_dashboard import _has_parentheses, person_name
from app.services.dashboard_cache import get_progress, set_progress

logger = logging.getLogger("uvicorn.error")

CACHE_PREFIX = "ad_helper:overview:v2"
TOP_N = 30

_job_locks: dict[str, asyncio.Lock] = {}


def progress_key(start: str, end: str) -> str:
    return f"{start}~{end}"


def _cache_key(person: str, start: str, end: str) -> str:
    return f"{CACHE_PREFIX}:{person}:{start}:{end}"


async def _read_cache(key: str) -> dict[str, Any] | None:
    try:
        r = get_cache_redis()
        raw = await r.get(key)
        if not raw:
            return None
        data = json.loads(raw)
        data["from_cache"] = True
        return data
    except Exception as exc:
        logger.warning("读总览缓存失败：%s", exc)
        return None


async def _write_cache(key: str, data: dict[str, Any]) -> None:
    try:
        r = get_cache_redis()
        payload = dict(data)
        payload["cached_at"] = datetime.now().isoformat(sep=" ", timespec="seconds")
        await r.set(key, json.dumps(payload, ensure_ascii=False), ex=36 * 3600)
    except Exception as exc:
        logger.warning("写总览缓存失败：%s", exc)


def _person_advertisers(db: Session) -> dict[str, list[int]]:
    """库里按人名分组子账户 ID。DB 读取都集中在这，之后协程不再碰库。"""
    people: dict[str, list[int]] = defaultdict(list)
    for row in db.scalars(select(QianchuanAdvertiser)).all():
        adv_name = row.advertiser_name or ""
        if not _has_parentheses(adv_name):
            continue
        name = person_name(adv_name)
        if not name:
            continue
        people[name].append(int(row.advertiser_id))
    return dict(people)


async def _heal_product_days(
    db: Session,
    client: Any,
    advertiser_id: int,
    start: str,
    end: str,
    gate: asyncio.Semaphore,
) -> int:
    """自愈：补齐区间内缺失的商品同步标记，返回实际打接口天数。

    委托 ensure_product_days：已有商品行/零消耗的日直接补标记（零接口），
    只有真正有消耗却未同步的日才逐日 sync_day。
    """
    missing = product_report_service.missing_days(db, advertiser_id, start, end)
    if not missing:
        return 0
    return await product_report_service.ensure_product_days(
        db, client, advertiser_id, missing, gate
    )


async def _fetch_advertiser_stats(
    db: Session,
    client: Any,
    advertiser_id: int,
    start: str,
    end: str,
    commission_map: dict[int, float],
    gate: asyncio.Semaphore,
) -> dict[str, Any]:
    # 1) 历史区间日数据齐全 → 直接 SQL 聚合，零接口。
    stats = product_report_service.stats_from_db(db, advertiser_id, start, end, commission_map)
    if stats is not None:
        return stats
    # 2) 自愈：补齐缺失的日后，再尝试读库。
    if await _heal_product_days(db, client, advertiser_id, start, end, gate):
        stats = product_report_service.stats_from_db(db, advertiser_id, start, end, commission_map)
        if stats is not None:
            return stats
    # 3) 仍不齐（存在无商品的历史空日，或自愈失败）→ 整区间打接口兜底。
    async with gate:
        rows = await product_report_service.fetch_products(client, advertiser_id, start, end)
    # 区间本身就是单天时顺手入库，多天只存主档（区间合计不能拆回日表）。
    if start == end:
        product_report_service.upsert_products(
            db, advertiser_id, rows, stat_date=datetime.strptime(start, "%Y-%m-%d").date()
        )
    else:
        product_report_service.upsert_products(db, advertiser_id, rows)
    return product_report_service.aggregate_products(rows, commission_map)


async def _get_person_stats(
    db: Session,
    client: Any,
    person: str,
    advertiser_ids: list[int],
    start: str,
    end: str,
    *,
    force: bool,
    gate: asyncio.Semaphore,
    commission_map: dict[int, float],
    tick=None,
) -> dict[str, Any]:
    """单人的商品聚合数据。同 key 并发只跑一次，后来者等锁后读缓存。

    tick(delta, msg)：进度回调，按账户粒度推进。
    """
    key = _cache_key(person, start, end)
    if not force:
        cached = await _read_cache(key)
        if cached is not None:
            if tick is not None:
                await tick(max(1, len(advertiser_ids)), f"{person}：走缓存")
            return cached
    lock = _job_locks.setdefault(key, asyncio.Lock())
    try:
        async with lock:
            if not force:
                cached = await _read_cache(key)
                if cached is not None:
                    if tick is not None:
                        await tick(max(1, len(advertiser_ids)), f"{person}：走缓存")
                    return cached

            async def run_one(adv_id: int) -> dict[str, Any]:
                try:
                    return await _fetch_advertiser_stats(
                        db, client, adv_id, start, end, commission_map, gate
                    )
                finally:
                    if tick is not None:
                        await tick(1, f"{person}：账户 {adv_id} 完成")

            results = await asyncio.gather(
                *(run_one(adv_id) for adv_id in advertiser_ids),
                return_exceptions=True,
            )
            profit = 0.0
            plan_cost = 0.0
            plan_count = 0
            products: dict[int, dict[str, Any]] = defaultdict(
                lambda: {"name": "无数据", "cost": 0.0, "sales": 0}
            )
            failed = 0
            for res in results:
                if isinstance(res, BaseException):
                    failed += 1
                    logger.warning("%s 账户商品数据拉取失败：%s", person, res)
                    continue
                profit += float(res["profit"])
                plan_cost += float(res["plan_cost"])
                plan_count += int(res["plan_count"])
                for pid, val in res["products"].items():
                    agg = products[int(pid)]
                    agg["name"] = val.get("name") or agg["name"]
                    agg["cost"] += float(val.get("cost") or 0)
                    agg["sales"] += int(val.get("sales") or 0)
            if advertiser_ids and failed == len(advertiser_ids):
                raise AppError(f"{person} 的商品数据全部拉取失败", code=50230)
            data = {
                "person": person,
                "profit": round(profit, 2),
                "plan_cost": round(plan_cost, 2),
                "plan_count": plan_count,
                "products": [
                    {"pid": pid, "name": v["name"], "cost": round(v["cost"], 2), "sales": v["sales"]}
                    for pid, v in products.items()
                ],
                "failed_advertisers": failed,
                "from_cache": False,
            }
            await _write_cache(key, data)
            return data
    finally:
        _job_locks.pop(key, None)


def _iter_days(start: str, end: str) -> list[str]:
    d0 = date.fromisoformat(start)
    d1 = date.fromisoformat(end)
    days: list[str] = []
    while d0 <= d1:
        days.append(d0.isoformat())
        d0 += timedelta(days=1)
    return days


async def _ensure_account_days(db: Session, start: str, end: str) -> None:
    """自愈：区间内缺失的账户日，逐日从工作台补入（一次拉全部账户）。"""
    for day in _iter_days(start, end):
        if account_report_service.has_account_day(db, day):
            continue
        try:
            await account_report_service.sync_account_day(db, day)
        except Exception as exc:  # noqa: BLE001
            logger.warning("账户日数据自愈失败 %s：%s", day, exc)


def _empty_result(start: str, end: str, note: str) -> dict[str, Any]:
    return {
        "start_date": start,
        "end_date": end,
        "kpis": {"total_cost": 0.0, "total_profit": 0.0, "people_count": 0},
        "people": [],
        "product_cost_top": [],
        "product_sales_top": [],
        "from_cache": False,
        "cached_at": None,
        "errors": [],
        "note": note,
    }


async def build_overview(
    db: Session,
    start: str,
    end: str,
    people: list[str] | None,
    *,
    force: bool = False,
) -> dict[str, Any]:
    settings = get_settings()
    # 总览彻底不含当天：end 夹到昨天；start 晚于昨天直接空结果。
    yesterday = (date.today() - timedelta(days=1)).isoformat()
    note = f"统计不含当天（今日实时波动），数据截止 {yesterday}"
    if end > yesterday:
        end = yesterday
    if start > end:
        await set_progress(progress_key(start, end), status="done", current=1, total=1, message=note)
        return _empty_result(start, end, note)

    prog_key = progress_key(start, end)
    # 先把进度条占住，前端立刻能看到当前阶段，而不是干等。
    await set_progress(prog_key, status="running", current=0, total=1, message="正在读取账户历史数据")
    # 1) 消耗：读账户日表 qianchuan_account_daily（缺失日自愈补拉，跑过回填脚本则零接口）。
    db_people = _person_advertisers(db)
    if people is not None:
        db_people = {k: v for k, v in db_people.items() if k in people}
    await _ensure_account_days(db, start, end)
    adv_person = {adv_id: person for person, ids in db_people.items() for adv_id in ids}
    cost_by_advertiser = account_report_service.account_cost_by_advertiser(db, start, end)
    cost_by_person: dict[str, float] = defaultdict(float)
    for adv_id, cost in cost_by_advertiser.items():
        name = adv_person.get(int(adv_id))
        if not name or name == "admin":
            continue
        cost_by_person[name] += float(cost or 0)

    # 2) 商品佣金
    commission_map = product_report_service.product_commissions(db)
    targets = sorted(set(cost_by_person) | set(db_people))

    client = await token_store.build_client(db)
    gate = asyncio.Semaphore(max(1, int(settings.DASHBOARD_CONCURRENCY or 2)))

    # 进度按「账户」推进，命中缓存的人也算完成。
    total = max(1, sum(max(1, len(db_people.get(p) or [])) for p in targets))
    done = 0
    await set_progress(prog_key, status="running", current=0, total=total, message="准备拉取商品数据")

    async def tick(delta: int, msg: str) -> None:
        nonlocal done
        done = min(total, done + max(0, int(delta)))
        await set_progress(prog_key, status="running", current=done, total=total, message=msg)

    async def one(person: str) -> tuple[str, dict[str, Any] | None, str | None]:
        try:
            data = await _get_person_stats(
                db,
                client,
                person,
                db_people.get(person) or [],
                start,
                end,
                force=force,
                gate=gate,
                commission_map=commission_map,
                tick=tick,
            )
            return person, data, None
        except Exception as exc:
            logger.exception("拉取 %s 总览数据失败：%s", person, exc)
            return person, None, str(exc)

    results = await asyncio.gather(*(one(p) for p in targets))
    await set_progress(prog_key, status="done", current=total, total=total, message="完成")

    # 3) 汇总
    errors: list[str] = []
    profit_by_person: dict[str, float] = {}
    product_bag: dict[int, dict[str, Any]] = {}
    from_cache_all = bool(targets)
    cached_at = ""
    for person, data, err in results:
        if data is None:
            errors.append(f"{person}：{err or '无数据'}")
            profit_by_person[person] = 0.0
            continue
        profit_by_person[person] = float(data.get("profit") or 0)
        for item in data.get("products") or []:
            pid = int(item.get("pid") or 0)
            agg = product_bag.setdefault(
                pid, {"name": str(item.get("name") or "无数据"), "cost": 0.0, "sales": 0}
            )
            agg["cost"] += float(item.get("cost") or 0)
            agg["sales"] += int(item.get("sales") or 0)
        from_cache_all = from_cache_all and bool(data.get("from_cache"))
        cached_at = max(cached_at, str(data.get("cached_at") or ""))

    people_rows = [
        {
            "name": p,
            "cost": round(cost_by_person.get(p, 0), 2),
            "profit": round(profit_by_person.get(p, 0), 2),
        }
        for p in targets
    ]
    people_rows.sort(key=lambda r: r["cost"], reverse=True)

    def _top(key: str) -> list[dict[str, Any]]:
        rows = [
            {
                "name": v["name"],
                "value": round(v[key], 2 if key == "cost" else 0),
                "pid": pid,
                "commission": commission_map.get(pid),
            }
            for pid, v in product_bag.items()
            if v[key] > 0
        ]
        rows.sort(key=lambda r: r["value"], reverse=True)
        return rows[:TOP_N]

    return {
        "start_date": start,
        "end_date": end,
        "kpis": {
            "total_cost": round(sum(r["cost"] for r in people_rows), 2),
            "total_profit": round(sum(r["profit"] for r in people_rows), 2),
            "people_count": len(people_rows),
        },
        "people": people_rows,
        "product_cost_top": _top("cost"),
        "product_sales_top": _top("sales"),
        "from_cache": from_cache_all,
        "cached_at": cached_at or None,
        "errors": errors,
        "note": note,
    }
