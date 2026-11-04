"""数据总览：消耗来自工作台账户表。昨天及以前的利润来自商品日表。

今天的利润按各账户计划相加。计划字段是消耗、支付 ROI、1 小时退款率、佣金。
区间包含今天时，历史利润和今天的计划利润分开算完再相加。

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
    daily_sync,
    overview_daily_service,
    product_report_service,
    today_plan_cache,
    token_store,
)
from app.services.advertiser_dashboard import _has_parentheses, load_roi2_accounts, person_name
from app.services.dashboard_cache import get_progress, set_progress

logger = logging.getLogger("uvicorn.error")

CACHE_PREFIX = "ad_helper:overview:v2"
TODAY_CACHE_PREFIX = "ad_helper:overview:today:v3"
# 商品日表从这一天起才有数。更早的日期不算利润，只提示。
PROFIT_FROM = date(2026, 10, 1)
TODAY_CACHE_SECONDS = 15 * 60
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


async def _write_cache(key: str, data: dict[str, Any], ttl: int = 36 * 3600) -> None:
    try:
        r = get_cache_redis()
        payload = dict(data)
        payload["cached_at"] = datetime.now().isoformat(sep=" ", timespec="seconds")
        await r.set(key, json.dumps(payload, ensure_ascii=False), ex=ttl)
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
        product_report_service.mark_product_synced(db, advertiser_id, start, len(rows))
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

            batch = product_report_service.person_aggregate_from_db(
                db, advertiser_ids, start, end, commission_map
            )
            if batch is not None:
                data = {
                    "person": person,
                    "profit": batch["profit"],
                    "plan_cost": batch["plan_cost"],
                    "plan_count": batch["plan_count"],
                    "products": batch["products"],
                    "failed_advertisers": 0,
                    "from_cache": False,
                }
                await _write_cache(key, data)
                if tick is not None:
                    await tick(max(1, len(advertiser_ids)), f"{person}：读库完成")
                return data

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
        "trend": None,
        "trend_person": None,
        "workbench": None,
    }


def _today_key(day: str) -> str:
    return f"{TODAY_CACHE_PREFIX}:{day}"


def _top_products(product_bag: dict[int, dict[str, Any]], key: str, commission_map: dict[int, float]) -> list[dict[str, Any]]:
    rows = [
        {
            "name": v["name"],
            "value": round(v[key], 2 if key == "cost" else 0),
            "pid": pid,
            "commission": _commission_of(v.get("commission"), pid, commission_map),
        }
        for pid, v in product_bag.items()
        if v[key] > 0
    ]
    rows.sort(key=lambda r: r["value"], reverse=True)
    return rows[:TOP_N]


def _person_trend(
    db: Session,
    person: str,
    start: str,
    end: str,
    today_rows: list[dict[str, Any]] | None,
) -> list[dict[str, Any]]:
    """单人按天的消耗和利润。今天用实时快照，昨天及以前读日表。缺的日子补 0。"""
    advertiser_ids = _person_advertisers(db).get(person) or []
    today = date.today().isoformat()
    include_today = start <= today <= end
    history_end = (date.today() - timedelta(days=1)).isoformat() if include_today else end
    costs: dict[str, float] = {}
    profits: dict[str, float] = {}
    if start <= history_end:
        costs = account_report_service.account_cost_by_day(db, advertiser_ids, start, history_end)
        profits = product_report_service.profit_by_day(db, advertiser_ids, start, history_end)
    if include_today:
        row = next((item for item in (today_rows or []) if item.get("name") == person), None)
        costs[today] = round(float(row["cost"]), 2) if row else 0.0
        profits[today] = round(float(row["profit"]), 2) if row else 0.0
    return [
        {
            "date": day,
            "cost": round(float(costs.get(day) or 0), 2),
            "profit": round(float(profits.get(day) or 0), 2),
        }
        for day in _iter_days(start, end)
    ]


def _finalize(
    start: str,
    end: str,
    people_rows: list[dict[str, Any]],
    product_bag: dict[int, dict[str, Any]],
    commission_map: dict[int, float],
    *,
    errors: list[str],
    from_cache: bool,
    cached_at: str,
    note: str,
    trend: list[dict[str, Any]] | None = None,
    trend_person: str | None = None,
) -> dict[str, Any]:
    people_rows = sorted(people_rows, key=lambda r: r["cost"], reverse=True)
    return {
        "start_date": start,
        "end_date": end,
        "kpis": {
            "total_cost": round(sum(r["cost"] for r in people_rows), 2),
            "total_profit": round(sum(r["profit"] for r in people_rows), 2),
            "people_count": len(people_rows),
        },
        "people": people_rows,
        "product_cost_top": _top_products(product_bag, "cost", commission_map),
        "product_sales_top": _top_products(product_bag, "sales", commission_map),
        "from_cache": from_cache,
        "cached_at": cached_at or None,
        "errors": errors,
        "note": note,
        "trend": trend,
        "trend_person": trend_person,
    }


def _commission_of(stored: Any, pid: int, commission_map: dict[int, float]) -> float:
    """缓存里已写上的佣金优先；没有、或是 0，再查库，仍没有就 60%。"""
    try:
        pct = float(stored)
    except (TypeError, ValueError):
        pct = 0.0
    if pct > 0:
        return pct
    return product_report_service.resolve_commission_pct(commission_map, pid)


def _add_products(bag: dict[int, dict[str, Any]], items: list[dict[str, Any]]) -> None:
    for item in items:
        pid = int(item.get("pid") or 0)
        agg = bag.setdefault(pid, {"name": str(item.get("name") or "无数据"), "cost": 0.0, "sales": 0})
        if item.get("name"):
            agg["name"] = str(item["name"])
        agg["cost"] += float(item.get("cost") or 0)
        agg["sales"] += int(item.get("sales") or 0)
        if item.get("commission") not in (None, ""):
            agg["commission"] = item.get("commission")


def _merge_people(*groups: list[dict[str, Any]]) -> list[dict[str, Any]]:
    bag: dict[str, dict[str, float]] = {}
    for rows in groups:
        for row in rows:
            cur = bag.setdefault(row["name"], {"cost": 0.0, "profit": 0.0})
            cur["cost"] += float(row.get("cost") or 0)
            cur["profit"] += float(row.get("profit") or 0)
    return [
        {"name": name, "cost": round(val["cost"], 2), "profit": round(val["profit"], 2)}
        for name, val in bag.items()
    ]


def _line_profit(prod: dict[str, Any], commission: float | None = None) -> float:
    basis = float(prod.get("basis") or 0)
    profit_cost = float(prod.get("profit_cost") or 0)
    pct = commission if commission is not None else float(prod.get("commission") or 0)
    if pct <= 0:
        pct = product_report_service.DEFAULT_COMMISSION_PCT
    return basis * pct / 100.0 - profit_cost


def _bucket_profit(products: list[dict[str, Any]]) -> float:
    return round(sum(_line_profit(prod) for prod in products), 2)


def _reprice_products(products: list[dict[str, Any]], product_id: int, pct: float) -> bool:
    hit = False
    for prod in products:
        if int(prod.get("pid") or 0) != int(product_id):
            continue
        prod["commission"] = pct
        prod["profit"] = round(_line_profit(prod, pct), 2)
        hit = True
    return hit


async def apply_today_commission(product_id: int, commission_pct: float) -> None:
    """只改今天缓存里这一个商品的佣金和利润，不重拉巨量。

    今天若已按计划格式发布，利润跟计划佣金走，这里不再按商品佣金重算。
    """
    pct = float(commission_pct or 0)
    if pct <= 0:
        pct = product_report_service.DEFAULT_COMMISSION_PCT
    key = _today_key(date.today().isoformat())
    try:
        r = get_cache_redis()
        raw = await r.get(key)
        if not raw:
            return
        data = json.loads(raw)
        if data.get("source") == "plans":
            return
        hit = False
        for item in (data.get("people") or {}).values():
            products = item.get("products") or []
            if not _reprice_products(products, product_id, pct):
                continue
            item["profit"] = _bucket_profit(products)
            hit = True
        for item in (data.get("accounts") or {}).values():
            products = item.get("products") or []
            if not _reprice_products(products, product_id, pct):
                continue
            item["profit"] = _bucket_profit(products)
            hit = True
        if not hit:
            return
        ttl = int(await r.ttl(key))
        if ttl < 1:
            ttl = TODAY_CACHE_SECONDS
        await r.set(key, json.dumps(data, ensure_ascii=False), ex=ttl)
    except Exception as exc:
        logger.warning("更新今天缓存里的商品佣金失败：%s", exc)


async def _compute_today(db: Session, today: str, prog_key: str | None) -> dict[str, Any]:
    """今天只打实时接口，不写日表。日表留给凌晨把已经结束的那一天入库。"""
    settings = get_settings()
    if prog_key:
        await set_progress(prog_key, status="running", current=0, total=1, message="正在读取今天的账户数据")
    accounts = await load_roi2_accounts(today, today)
    db_people = _person_advertisers(db)
    cost_by_person: dict[str, float] = defaultdict(float)
    for item in accounts:
        account_name = str(item.get("advertiser_name") or "")
        if not _has_parentheses(account_name):
            continue
        who = person_name(account_name)
        if not who:
            continue
        cost_by_person[who] += float(item.get("stat_cost") or 0)

    commission_map = product_report_service.product_commissions(db)
    client = await token_store.build_client(db)
    gate = asyncio.Semaphore(max(1, int(settings.DASHBOARD_CONCURRENCY or 2)))
    targets = sorted(set(cost_by_person) | set(db_people))
    total = max(1, sum(max(1, len(db_people.get(p) or [])) for p in targets))
    done = 0

    async def one_adv(adv_id: int) -> list[dict[str, Any]]:
        async with gate:
            return await product_report_service.fetch_products(client, adv_id, today, today)

    people_out: dict[str, Any] = {}
    accounts_out: dict[str, Any] = {}
    errors: list[str] = []
    for person in targets:
        adv_ids = db_people.get(person) or []
        results = await asyncio.gather(*(one_adv(adv_id) for adv_id in adv_ids), return_exceptions=True)
        rows: list[dict[str, Any]] = []
        failed = 0
        for adv_id, res in zip(adv_ids, results):
            if isinstance(res, BaseException):
                failed += 1
                logger.warning("%s 今天的商品数据拉取失败：%s", person, res)
                continue
            shop = product_report_service.rollup_today_rows(res, commission_map)
            products = [{"pid": pid, **val} for pid, val in shop.items()]
            accounts_out[str(adv_id)] = {"profit": _bucket_profit(products), "products": products}
            rows.extend(res)
        rolled = product_report_service.rollup_today_rows(rows, commission_map)
        products = [{"pid": pid, **val} for pid, val in rolled.items()]
        if adv_ids and failed == len(adv_ids):
            errors.append(f"{person}：今天的商品数据没有拉到")
        people_out[person] = {
            "cost": round(cost_by_person.get(person, 0), 2),
            "profit": _bucket_profit(products),
            "products": products,
        }
        done = min(total, done + max(1, len(adv_ids)))
        if prog_key:
            await set_progress(prog_key, status="running", current=done, total=total, message=f"{person}：今天完成")
    if prog_key:
        await set_progress(prog_key, status="done", current=total, total=total, message="完成")
    return {
        "day": today,
        "people": people_out,
        "accounts": accounts_out,
        "errors": errors,
        "from_cache": False,
    }


async def today_account_profits() -> dict[int, float] | None:
    """读今天缓存里每个店铺的利润。缓存还没有分店铺数据时返回 None，不打接口。"""
    cached = await _read_cache(_today_key(date.today().isoformat()))
    if not cached or "accounts" not in cached:
        return None
    return {
        int(adv_id): float(item.get("profit") or 0)
        for adv_id, item in (cached.get("accounts") or {}).items()
    }


async def today_plan_profit_overlay(db: Session) -> dict[int, float]:
    """已经拉过计划的账户，利润改成这些计划相加，和计划页同一笔数。"""
    day = date.today().isoformat()
    ids = [int(row) for row in db.scalars(select(QianchuanAdvertiser.advertiser_id)).all()]
    cached = await today_plan_cache.mget_accounts(day, ids)
    return {
        adv_id: sum_plan_profits(list(item.get("plans") or []))
        for adv_id, item in cached.items()
    }


async def shop_profits(db: Session, start: str, end: str) -> tuple[dict[int, float] | None, str | None]:
    """每个店铺的利润。今天有计划缓存时，按计划页同一笔算法相加；没有则用总览缓存。昨天及以前读日表。

    整段都早于 2026-10-01 时返回 None，调用方显示横杠。
    区间跨过 10 月 1 日时，只从这一天起算，并带上提示。
    """
    start_d = date.fromisoformat(start)
    end_d = date.fromisoformat(end)
    today = date.today()
    if end_d < PROFIT_FROM:
        return None, "没有10月份以前的数据"
    note = None
    if start_d < PROFIT_FROM:
        start_d = PROFIT_FROM
        note = "没有10月份以前的数据，利润从 2026-10-01 开始计算"
    include_today = start_d <= today <= end_d
    yesterday = today - timedelta(days=1)
    hist_end = yesterday if end_d >= today else end_d
    profits: dict[int, float] = defaultdict(float)
    have = False
    if start_d <= hist_end:
        for adv_id, val in product_report_service.profits_by_advertiser(
            db, start_d.isoformat(), hist_end.isoformat()
        ).items():
            profits[adv_id] += val
        have = True
    if include_today:
        today_map = await today_account_profits()
        plan_map = await today_plan_profit_overlay(db)
        if today_map is None and not plan_map:
            extra = "今天的利润缓存还没有，这次不含今天"
            note = f"{note}。{extra}" if note else extra
        else:
            merged = dict(today_map or {})
            merged.update(plan_map)
            for adv_id, val in merged.items():
                profits[adv_id] += val
            have = True
    if not have:
        return None, note
    return {adv_id: round(val, 2) for adv_id, val in profits.items()}, note


def plan_line_profit(plan: dict[str, Any]) -> float | None:
    """和计划页同一笔算法。

    佣金没填或是 0 时这条不计入，页面上显示横杠。
    支付 ROI 为空时按 0，利润就是负的消耗。退款率再加 5%。
    """
    try:
        pct = float(plan.get("commission_pct"))
    except (TypeError, ValueError):
        return None
    if not pct or pct <= 0:
        return None
    try:
        cost = float(plan.get("stat_cost"))
    except (TypeError, ValueError):
        return None
    roi_raw = plan.get("pay_roi")
    if roi_raw in (None, ""):
        roi_f = 0.0
    else:
        try:
            roi_f = float(roi_raw)
        except (TypeError, ValueError):
            return None
    rate = plan.get("refund_rate_1h")
    if rate in (None, ""):
        return None
    try:
        rate_f = float(rate)
    except (TypeError, ValueError):
        return None
    if rate_f < 0:
        return None
    if rate_f > 1 and rate_f <= 100:
        rate_f = rate_f / 100.0
    if rate_f > 1:
        return None
    refund = min(1.0, rate_f + product_report_service.REFUND_EXTRA)
    return cost * roi_f * (1.0 - refund) * pct / 100.0 - cost


def sum_plan_profits(plans: list[dict[str, Any]]) -> float:
    total = 0.0
    for plan in plans:
        line = plan_line_profit(plan)
        if line is not None:
            total += line
    return round(total, 2)


def _sales_from_product_rows(
    rows: list[dict[str, Any]] | None,
) -> tuple[dict[int, int], dict[int, str]]:
    sales: dict[int, int] = {}
    names: dict[int, str] = {}
    for row in rows or []:
        try:
            pid = int(row.get("product_id") or 0)
        except (TypeError, ValueError):
            continue
        if not pid:
            continue
        sales[pid] = sales.get(pid, 0) + int(row.get("order_count") or 0)
        if row.get("product_name"):
            names[pid] = str(row["product_name"])
    return sales, names


def rollup_plans(
    plans: list[dict[str, Any]],
    sales_by_pid: dict[int, int] | None = None,
    names: dict[int, str] | None = None,
) -> tuple[list[dict[str, Any]], float]:
    """把一个账户的计划按商品收成总览用的商品行。利润是每条计划单独算完再加。"""
    sales_by_pid = dict(sales_by_pid or {})
    names = dict(names or {})
    bucket: dict[int, dict[str, Any]] = {}
    profit_sum = 0.0
    attached: set[int] = set()
    for plan in plans:
        line = plan_line_profit(plan)
        if line is not None:
            profit_sum += line
        raw_pid = plan.get("product_id")
        try:
            pid = int(raw_pid) if raw_pid not in (None, "", 0) else 0
        except (TypeError, ValueError):
            pid = 0
        if not pid:
            continue
        agg = bucket.setdefault(
            pid,
            {"name": str(plan.get("product_name") or names.get(pid) or ""), "cost": 0.0, "profit": 0.0},
        )
        if plan.get("product_name"):
            agg["name"] = str(plan["product_name"])
        agg["cost"] += float(plan.get("stat_cost") or 0)
        if line is not None:
            agg["profit"] += line
        if pid not in attached:
            attached.add(pid)
    products: list[dict[str, Any]] = []
    for pid, agg in bucket.items():
        products.append(
            {
                "pid": pid,
                "name": agg["name"] or product_report_service.display_name(pid, names.get(pid)),
                "cost": round(float(agg["cost"]), 2),
                "sales": int(sales_by_pid.get(pid) or 0),
                "profit": round(float(agg["profit"]), 2),
            }
        )
    for pid, sales in sales_by_pid.items():
        if int(pid) in bucket or int(sales or 0) <= 0:
            continue
        products.append(
            {
                "pid": int(pid),
                "name": product_report_service.display_name(int(pid), names.get(int(pid))),
                "cost": 0.0,
                "sales": int(sales),
                "profit": 0.0,
            }
        )
    return products, round(profit_sum, 2)


def account_from_plans(
    plans: list[dict[str, Any]],
    product_rows: list[dict[str, Any]] | None = None,
    keep_sales: dict[int, int] | None = None,
) -> dict[str, Any]:
    sales, names = _sales_from_product_rows(product_rows)
    for pid, count in (keep_sales or {}).items():
        sales.setdefault(int(pid), int(count))
    products, profit = rollup_plans(plans, sales, names)
    return {"profit": profit, "products": products}


def _person_from_accounts(
    person: str,
    adv_ids: list[int],
    accounts: dict[str, Any],
    cost: float,
) -> dict[str, Any]:
    bag: dict[int, dict[str, Any]] = {}
    profit = 0.0
    for adv_id in adv_ids:
        item = accounts.get(str(adv_id)) or {}
        profit += float(item.get("profit") or 0)
        _add_products(bag, item.get("products") or [])
    return {
        "cost": round(float(cost), 2),
        "profit": round(profit, 2),
        "products": [
            {
                "pid": pid,
                "name": val["name"],
                "cost": round(float(val["cost"]), 2),
                "sales": int(val["sales"]),
            }
            for pid, val in bag.items()
        ],
    }


def assemble_today_snapshot(
    db: Session,
    today: str,
    workbench_accounts: list[dict[str, Any]],
    plans_by_adv: dict[int, list[dict[str, Any]]],
    products_by_adv: dict[int, list[dict[str, Any]]] | None = None,
) -> dict[str, Any]:
    """用已经拉好的工作台账户和各账户计划拼今天的总览。不再打接口。

    消耗、支付、净成交、ROI 用工作台。利润用计划。商品销量仍用商品报表的订单数。
    """
    products_by_adv = products_by_adv or {}
    db_people = _person_advertisers(db)
    board = overview_daily_service.rollup_people(workbench_accounts)
    targets = sorted({key for key in board if key} | set(db_people))
    people_out: dict[str, Any] = {}
    accounts_out: dict[str, Any] = {}
    errors: list[str] = []
    for person in targets:
        adv_ids = db_people.get(person) or []
        failed = 0
        for adv_id in adv_ids:
            plans = plans_by_adv.get(int(adv_id))
            if plans is None:
                failed += 1
                continue
            accounts_out[str(adv_id)] = account_from_plans(
                plans, products_by_adv.get(int(adv_id))
            )
        if adv_ids and failed == len(adv_ids):
            errors.append(f"{person}：今天的计划没有拉到")
        cost = float((board.get(person) or {}).get("stat_cost") or 0)
        people_out[person] = _person_from_accounts(person, adv_ids, accounts_out, cost)
    return {
        "day": today,
        "source": "plans",
        "workbench": board,
        "people": people_out,
        "accounts": accounts_out,
        "errors": errors,
        "from_cache": False,
    }


def _workbench_slice(snapshot: dict[str, Any], people: list[str] | None) -> dict[str, Any] | None:
    """从已发布的今天缓存里取出工作台汇总。旧缓存没有这份字段时返回 None。"""
    board = snapshot.get("workbench")
    if not isinstance(board, dict) or not board:
        if snapshot.get("source") == "plans" or snapshot.get("pending"):
            return overview_daily_service.empty_metrics()
        return None
    if not people:
        found = board.get(overview_daily_service.ALL_PERSON)
        return found if isinstance(found, dict) else overview_daily_service.empty_metrics()
    if len(people) == 1:
        found = board.get(people[0])
        return found if isinstance(found, dict) else overview_daily_service.empty_metrics()
    total = overview_daily_service.empty_metrics()
    for name in people:
        part = board.get(name)
        if isinstance(part, dict):
            total = overview_daily_service._finish_sum(total, part)
    return total


async def patch_today_account(db: Session, advertiser_id: int, plans: list[dict[str, Any]]) -> None:
    """只按这一个账户的计划重算它的利润和商品，其他账户保持不动。"""
    adv_id = int(advertiser_id)
    people = _person_advertisers(db)
    person = next((name for name, ids in people.items() if adv_id in ids), "")
    if not person:
        return
    key = _today_key(date.today().isoformat())
    try:
        r = get_cache_redis()
        raw = await r.get(key)
        if not raw:
            return
        data = json.loads(raw)
        if data.get("day") != date.today().isoformat():
            return
        accounts = data.setdefault("accounts", {})
        old = accounts.get(str(adv_id)) or {}
        old_profit = float(old.get("profit") or 0)
        keep_sales = {
            int(item.get("pid") or 0): int(item.get("sales") or 0)
            for item in (old.get("products") or [])
            if int(item.get("pid") or 0)
        }
        new_account = account_from_plans(plans, keep_sales=keep_sales)
        accounts[str(adv_id)] = new_account
        people_rows = data.setdefault("people", {})
        if data.get("source") == "plans":
            cost = float((people_rows.get(person) or {}).get("cost") or 0)
            people_rows[person] = _person_from_accounts(
                person, people.get(person) or [adv_id], accounts, cost
            )
        else:
            row = people_rows.get(person) or {"cost": 0.0, "profit": 0.0, "products": []}
            row["profit"] = round(float(row.get("profit") or 0) - old_profit + float(new_account["profit"]), 2)
            people_rows[person] = row
        ttl = int(await r.ttl(key))
        if ttl < 1:
            ttl = 36 * 3600
        await r.set(key, json.dumps(data, ensure_ascii=False), ex=ttl)
    except Exception as exc:
        logger.warning("按单个账户更新今天总览失败：%s", exc)


async def publish_today_snapshot(data: dict[str, Any]) -> None:
    """整份替换今天的总览缓存。调用方只有在全部账户都拉齐之后才调用。"""
    day = str(data.get("day") or date.today().isoformat())
    await _write_cache(_today_key(day), data, 36 * 3600)


async def today_cache_ready() -> bool:
    """今天的 15 分钟缓存里已经有总览数据时返回 True。"""
    today = date.today().isoformat()
    cached = await _read_cache(_today_key(today))
    if not cached or cached.get("day") != today:
        return False
    return bool(cached.get("people") or cached.get("accounts") or cached.get("workbench"))


async def load_today_snapshot(db: Session, *, force: bool = False, prog_key: str | None = None) -> dict[str, Any]:
    """只读已经发布的今天缓存。页面请求不重新拉取，避免碰到正在跑的定时刷新。"""
    del db, force, prog_key
    today = date.today().isoformat()
    cached = await _read_cache(_today_key(today))
    if cached is not None and cached.get("day") == today:
        return cached
    return {
        "day": today,
        "people": {},
        "accounts": {},
        "errors": ["今天的数据正在汇总，请稍后再查看"],
        "from_cache": False,
        "pending": True,
    }


def _slice_today(snapshot: dict[str, Any], people: list[str] | None) -> tuple[list[dict[str, Any]], dict[int, dict[str, Any]], list[str]]:
    raw = snapshot.get("people") or {}
    allow = None if people is None else set(people)
    rows: list[dict[str, Any]] = []
    bag: dict[int, dict[str, Any]] = {}
    for name, item in raw.items():
        if allow is not None and name not in allow:
            continue
        rows.append({
            "name": name,
            "cost": round(float(item.get("cost") or 0), 2),
            "profit": round(float(item.get("profit") or 0), 2),
        })
        _add_products(bag, item.get("products") or [])
    errors = [err for err in (snapshot.get("errors") or []) if allow is None or err.split("：", 1)[0] in allow]
    return rows, bag, errors


async def _build_stored_range(
    db: Session,
    start: str,
    end: str,
    people: list[str] | None,
    *,
    force: bool,
    prog_key: str,
) -> dict[str, Any]:
    """昨天及以前：消耗和商品都读日表，缺的日子才补拉并入库。"""
    settings = get_settings()
    await set_progress(prog_key, status="running", current=0, total=1, message="正在读取账户历史数据")
    db_people = _person_advertisers(db)
    if people is not None:
        db_people = {k: v for k, v in db_people.items() if k in people}
    await _ensure_account_days(db, start, end)
    adv_person = {adv_id: person for person, ids in db_people.items() for adv_id in ids}
    cost_by_advertiser = account_report_service.account_cost_by_advertiser(db, start, end)
    cost_by_person: dict[str, float] = defaultdict(float)
    for adv_id, cost in cost_by_advertiser.items():
        name = adv_person.get(int(adv_id))
        if not name:
            continue
        cost_by_person[name] += float(cost or 0)

    commission_map = product_report_service.product_commissions(db)
    targets = sorted(set(cost_by_person) | set(db_people))
    client = await token_store.build_client(db)
    gate = asyncio.Semaphore(max(1, int(settings.DASHBOARD_CONCURRENCY or 2)))
    total = max(1, sum(max(1, len(db_people.get(p) or [])) for p in targets))
    done = 0
    await set_progress(prog_key, status="running", current=0, total=total, message="准备读取商品日数据")

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
        _add_products(product_bag, data.get("products") or [])
        from_cache_all = from_cache_all and bool(data.get("from_cache"))
        cached_at = max(cached_at, str(data.get("cached_at") or ""))
    people_rows = [
        {
            "name": person,
            "cost": round(cost_by_person.get(person, 0), 2),
            "profit": round(profit_by_person.get(person, 0), 2),
        }
        for person in targets
    ]
    return {
        "people": people_rows,
        "products": product_bag,
        "errors": errors,
        "from_cache": from_cache_all,
        "cached_at": cached_at,
        "commission_map": commission_map,
    }


async def build_overview(
    db: Session,
    start: str,
    end: str,
    people: list[str] | None,
    *,
    force: bool = False,
) -> dict[str, Any]:
    await daily_sync.ensure_history_ready_for_overview(db, start, end)
    today_d = date.today()
    today = today_d.isoformat()
    yesterday = (today_d - timedelta(days=1)).isoformat()
    if end > today:
        end = today
    if start > end:
        note = "开始日期晚于结束日期"
        await set_progress(progress_key(start, end), status="done", current=1, total=1, message=note)
        return _empty_result(start, end, note)

    include_today = start <= today <= end
    history_end = yesterday if end >= today else end
    has_history = start <= history_end
    prog_key = progress_key(start, end)
    commission_map = product_report_service.product_commissions(db)

    history = None
    if has_history:
        history = await _build_stored_range(
            db, start, history_end, people, force=force, prog_key=prog_key
        )
        commission_map = history["commission_map"]

    today_rows: list[dict[str, Any]] = []
    today_products: dict[int, dict[str, Any]] = {}
    today_errors: list[str] = []
    today_cache = False
    today_at = ""
    snapshot: dict[str, Any] | None = None
    if include_today:
        if not has_history:
            await set_progress(prog_key, status="running", current=0, total=1, message="正在读取今天的实时数据")
        # 今天只读 15 分钟缓存。重拉交给定时任务，页面上的刷新不能把 152 个账户再打一遍。
        snapshot = await load_today_snapshot(
            db, force=False, prog_key=None if has_history else prog_key
        )
        today_rows, today_products, today_errors = _slice_today(snapshot, people)
        today_cache = bool(snapshot.get("from_cache"))
        today_at = str(snapshot.get("cached_at") or "")

    history_rows = history["people"] if history else []
    product_bag: dict[int, dict[str, Any]] = dict(history["products"]) if history else {}
    _add_products(product_bag, [
        {
            "pid": pid,
            "name": val["name"],
            "cost": val["cost"],
            "sales": val["sales"],
            "commission": val.get("commission"),
        }
        for pid, val in today_products.items()
    ])
    errors = list(history["errors"] if history else []) + today_errors
    if has_history and include_today:
        note = "昨天及以前的利润按商品日表相加；今天的利润按各账户计划相加。消耗、支付、净成交用工作台缓存。今天不写入日表。"
        from_cache = bool(history["from_cache"]) and today_cache
        cached_at = max(history["cached_at"], today_at)
    elif include_today:
        note = "今天的利润按各账户计划相加，消耗、支付、净成交用工作台缓存。后台每 15 分钟全部账户拉齐后才替换。这一天不写入日表。"
        from_cache = today_cache
        cached_at = today_at
    else:
        note = f"所选日期都已入库，数据截止 {end}。"
        from_cache = bool(history["from_cache"]) if history else False
        cached_at = history["cached_at"] if history else ""
    await set_progress(prog_key, status="done", current=1, total=1, message="完成")
    trend = None
    trend_person = None
    if people and len(people) == 1 and start < end:
        trend_person = people[0]
        trend = _person_trend(db, trend_person, start, end, today_rows if include_today else None)
    result = _finalize(
        start,
        end,
        _merge_people(history_rows, today_rows),
        product_bag,
        commission_map,
        errors=errors,
        from_cache=from_cache,
        cached_at=cached_at,
        note=note,
        trend=trend,
        trend_person=trend_person,
    )
    person = people[0] if people and len(people) == 1 else None
    today_part = _workbench_slice(snapshot, people) if include_today and snapshot is not None else None
    fetch_today = bool(include_today and today_part is None and snapshot is not None and not snapshot.get("pending"))
    try:
        metrics = await overview_daily_service.metrics_for_range(
            db,
            result["start_date"],
            result["end_date"],
            person,
            today_part,
            fetch_today=fetch_today,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("总览工作台汇总失败：%s", exc)
        result["workbench"] = None
        return result
    result["workbench"] = overview_daily_service.with_profit_rate(metrics, result["kpis"]["total_profit"])
    result["kpis"]["total_cost"] = metrics["stat_cost"]
    return result