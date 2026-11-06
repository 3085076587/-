"""数据总览：消耗来自工作台账户表。昨天及以前的利润来自商品日表。

利润 = 净成交金额 × (1 − 实际退款率) × 佣金率 − 消耗。
佣金先用手填，没有再解析名称，都没有的商品不计入。
今天用 Redis 里的计划净成交和消耗，历史用数据库里的商品日表。总利润是这些商品相加。

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
    plan_commission_sync,
    product_report_service,
    today_plan_cache,
    token_store,
)
from app.services.advertiser_dashboard import (
    _has_parentheses,
    load_roi2_accounts,
    person_name,
    take_roi2_total,
)
from app.services.dashboard_cache import get_progress, set_progress

logger = logging.getLogger("uvicorn.error")

CACHE_PREFIX = "ad_helper:overview:v6"
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


def _people_scope(
    db: Session,
    people: list[str] | None,
    allowed_ids: set[int] | None = None,
) -> dict[str, list[int]]:
    """按人和可见店铺收窄账户。额外授权的店并进当前这个人。"""
    mapping = {name: list(ids) for name, ids in _person_advertisers(db).items()}
    if people is not None:
        allow_names = set(people)
        mapping = {name: ids for name, ids in mapping.items() if name in allow_names}
    if allowed_ids is None:
        return mapping
    allow = {int(item) for item in allowed_ids}
    scoped: dict[str, list[int]] = {}
    seen: set[int] = set()
    for name, ids in mapping.items():
        kept = [int(item) for item in ids if int(item) in allow]
        seen.update(kept)
        if kept:
            scoped[name] = kept
    extra = [item for item in allow if item not in seen]
    if extra and people and len(people) == 1:
        scoped.setdefault(people[0], [])
        scoped[people[0]].extend(extra)
    return scoped


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
    name_commission: dict[tuple[int, int], float] | None = None,
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
                db, advertiser_ids, start, end, commission_map, name_commission
            )
            data = {
                "person": person,
                "profit": batch["profit"],
                "plan_cost": batch["plan_cost"],
                "plan_count": batch["plan_count"],
                "products": batch["products"],
                "failed_advertisers": 0,
                "from_cache": False,
            }
            if batch.get("complete"):
                await _write_cache(key, data)
            if tick is not None:
                await tick(max(1, len(advertiser_ids)), f"{person}：读库完成")
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
    name_commission: dict[tuple[int, int], float] | None = None,
    allowed_ids: set[int] | None = None,
) -> list[dict[str, Any]]:
    """单人按天的消耗和利润。今天用实时快照，昨天及以前读日表。缺的日子补 0。"""
    advertiser_ids = _people_scope(db, [person], allowed_ids).get(person) or []
    today = date.today().isoformat()
    include_today = start <= today <= end
    history_end = (date.today() - timedelta(days=1)).isoformat() if include_today else end
    costs: dict[str, float] = {}
    profits: dict[str, float] = {}
    if start <= history_end:
        costs = account_report_service.account_cost_by_day(db, advertiser_ids, start, history_end)
        profits = product_report_service.profit_by_day(
            db, advertiser_ids, start, history_end, name_commission=name_commission
        )
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


def _scope_advertiser_ids(
    db: Session,
    people: list[str] | None,
    allowed_ids: set[int] | None = None,
) -> list[int]:
    ids: list[int] = []
    for group in _people_scope(db, people, allowed_ids).values():
        ids.extend(int(item) for item in group)
    return ids


def _daily_trend(
    db: Session,
    start: str,
    end: str,
    people: list[str] | None,
    today_rows: list[dict[str, Any]] | None,
    today_board: dict[str, Any] | None,
    name_commission: dict[tuple[int, int], float] | None,
    allowed_ids: set[int] | None = None,
) -> list[dict[str, Any]]:
    """区间内每一天的汇总。金额来自账户日表，预估利润按当天商品相加。今天用已发布的工作台和商品利润。"""
    today = date.today().isoformat()
    include_today = start <= today <= end
    history_end = (date.today() - timedelta(days=1)).isoformat() if include_today else end
    money: dict[str, dict[str, float]] = {}
    profits: dict[str, float] = {}
    if start <= history_end:
        advertiser_ids = _scope_advertiser_ids(db, people, allowed_ids)
        money = account_report_service.account_money_by_day(db, advertiser_ids, start, history_end)
        profits = product_report_service.profit_by_day(
            db, advertiser_ids, start, history_end, name_commission=name_commission
        )
    if include_today:
        board = today_board or {}
        cost = float(board.get("stat_cost") or 0)
        pay = float(board.get("pay_amount") or 0)
        settle = float(board.get("settle_amount") or 0)
        if not board:
            cost = sum(float(row.get("cost") or 0) for row in (today_rows or []))
        money[today] = {
            "cost": round(cost, 2),
            "pay_amount": round(pay, 2),
            "settle_amount": round(settle, 2),
        }
        profits[today] = round(sum(float(row.get("profit") or 0) for row in (today_rows or [])), 2)
    points: list[dict[str, Any]] = []
    for day in _iter_days(start, end):
        item = money.get(day) or {}
        pay = float(item.get("pay_amount") or 0)
        profit = float(profits.get(day) or 0)
        points.append(
            {
                "date": day,
                "cost": round(float(item.get("cost") or 0), 2),
                "profit": round(profit, 2),
                "pay_amount": round(pay, 2),
                "settle_amount": round(float(item.get("settle_amount") or 0), 2),
                "profit_rate": round(profit / pay, 6) if pay > 0 else None,
            }
        )
    return points


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
        "daily_trend": [],
    }


def _commission_of(stored: Any, pid: int, commission_map: dict[int, float]) -> float:
    """缓存里已写上的手填佣金优先，没有再查库。都没有就是 0。"""
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
        if item.get("settle") not in (None, ""):
            agg["settle"] = float(agg.get("settle") or 0) + float(item.get("settle") or 0)
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
    pct = commission if commission is not None else prod.get("commission")
    name = str(prod.get("name") or "")
    resolved = product_report_service.commission_from_saved_or_name(pct, name)
    if resolved is None:
        return 0.0
    if prod.get("settle") not in (None, ""):
        value = product_report_service.estimate_profit(
            prod.get("settle"),
            prod.get("cost"),
            prod.get("refund"),
            resolved,
        )
        return 0.0 if value is None else value
    basis = float(prod.get("basis") or 0)
    profit_cost = float(prod.get("profit_cost") or 0)
    return basis * resolved / 100.0 - profit_cost


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
        return
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


async def _plan_name_commissions(
    db: Session, advertiser_ids: list[int]
) -> dict[tuple[int, int], float]:
    """今天计划缓存里的佣金：手填优先，否则用计划名称。历史日表按商品套用。"""
    ids = [int(item) for item in advertiser_ids]
    if not ids:
        return {}
    caches = await today_plan_cache.mget_accounts(date.today().isoformat(), ids)
    users = plan_commission_sync.user_commissions_by_advertiser(db, ids)
    out: dict[tuple[int, int], float] = {}
    for adv_id, payload in caches.items():
        by_product = _commission_by_product(payload.get("plans") or [], users.get(int(adv_id)) or {})
        for pid, pct in by_product.items():
            out[(int(adv_id), int(pid))] = float(pct)
    return out


def _plans_have_money(plans: list[dict[str, Any]]) -> bool:
    for plan in plans:
        if plan.get("stats_from_product"):
            continue
        if float(plan.get("stat_cost") or 0) > 0 or float(plan.get("settle_amount") or 0) > 0:
            return True
    return False


def _commission_by_product(
    plans: list[dict[str, Any]], user_by_ad: dict[int, float]
) -> dict[int, float]:
    """同一商品：手填佣金优先，否则用计划名称里的佣金。"""
    found: dict[int, float] = {}
    for plan in plans:
        try:
            pid = int(plan.get("product_id") or 0)
            ad_id = int(plan.get("ad_id") or 0)
        except (TypeError, ValueError):
            continue
        if not pid:
            continue
        typed = user_by_ad.get(ad_id)
        pct = product_report_service.commission_from_saved_or_name(
            typed,
            str(plan.get("ad_name") or ""),
            str(plan.get("product_name") or ""),
        )
        if typed and pct:
            found[pid] = pct
        elif pid not in found and pct:
            found[pid] = pct
    return found


def account_from_product_rows(
    rows: list[dict[str, Any]],
    plans: list[dict[str, Any]],
    meta_map: dict[tuple[int, int], dict[str, Any]],
    advertiser_id: int,
    user_by_ad: dict[int, float],
) -> dict[str, Any]:
    """商品报表上的消耗、净成交、销量。计划列表只提供名称里的佣金。"""
    names = _commission_by_product(plans, user_by_ad)
    products: list[dict[str, Any]] = []
    profit = 0.0
    for row in rows or []:
        try:
            pid = int(row.get("product_id") or 0)
        except (TypeError, ValueError):
            continue
        if not pid:
            continue
        meta = meta_map.get((int(advertiser_id), pid))
        name = str(row.get("product_name") or (meta or {}).get("name") or "")
        cost = float(row.get("cost") or 0)
        settle = row.get("settle_amount")
        value = product_report_service.profit_of_product(
            settle=settle,
            cost=cost,
            meta=meta,
            product_name=name,
            user_commission=names.get(pid),
        )
        if value is not None:
            profit += value
        item = {
            "pid": pid,
            "name": name or product_report_service.display_name(pid, name),
            "cost": round(cost, 2),
            "sales": int(row.get("order_count") or 0),
        }
        if settle not in (None, ""):
            item["settle"] = round(float(settle), 2)
        if names.get(pid):
            item["commission"] = names[pid]
        products.append(item)
    return {"profit": round(profit, 2), "products": products}


async def today_plan_profit_overlay(db: Session) -> dict[int, float]:
    """计划缓存里真有消耗或净成交时，才用计划覆盖今天的利润。全是 0 的不覆盖。"""
    from app.services.plan_commission_sync import user_commissions_by_advertiser

    day = date.today().isoformat()
    ids = [int(row) for row in db.scalars(select(QianchuanAdvertiser.advertiser_id)).all()]
    cached = await today_plan_cache.mget_accounts(day, ids)
    users = user_commissions_by_advertiser(db, list(cached))
    meta = product_report_service.load_product_meta(db, list(cached))
    out: dict[int, float] = {}
    for adv_id, item in cached.items():
        plans = list(item.get("plans") or [])
        if not _plans_have_money(plans):
            continue
        out[int(adv_id)] = product_report_service.sum_plan_product_profits(
            plans,
            users.get(int(adv_id), {}),
            meta,
            int(adv_id),
        )
    return out


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
        ids = [int(row) for row in db.scalars(select(QianchuanAdvertiser.advertiser_id)).all()]
        name_commission = await _plan_name_commissions(db, ids)
        for adv_id, val in product_report_service.profits_by_advertiser(
            db, start_d.isoformat(), hist_end.isoformat(), name_commission
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


def _preset_range_key(start: str, end: str) -> str | None:
    """和账户授权的今天 / 7天 / 30天是同一段时，返回那个缓存键。"""
    today = date.today()
    if start == end == today.isoformat():
        return "today"
    if end == today.isoformat() and start == (today - timedelta(days=6)).isoformat():
        return "7"
    if end == today.isoformat() and start == (today - timedelta(days=29)).isoformat():
        return "30"
    return None


async def _accounts_matching_advertiser_page(
    db: Session,
    start: str,
    end: str,
    people: list[str] | None,
) -> list[dict[str, Any]]:
    """读账户授权正在用的那份账户表。预设区间走同一份缓存，自定义日期再拉工作台。"""
    from app.services import dashboard_cache
    from app.services.advertiser_dashboard import dashboard_from_accounts, list_people

    names = list(people) if people else list_people(db)
    key = _preset_range_key(start, end)
    if key and names:
        data = await dashboard_cache.fetch_people(db, key, names, force=False, scope_all=False)
        return list(data.get("accounts") or [])
    rows = await load_roi2_accounts(start, end)
    only = names[0] if people and len(people) == 1 else None
    accounts = list(dashboard_from_accounts(rows, start, end, only).get("accounts") or [])
    if people and len(people) != 1:
        allow = set(people)
        accounts = [
            item
            for item in accounts
            if person_name(str(item.get("advertiser_name") or "")) in allow
        ]
    return accounts


async def align_with_advertiser_page(
    db: Session,
    start: str,
    end: str,
    people: list[str] | None,
    allowed_ids: set[int] | None,
) -> dict[str, Any] | None:
    """总消耗、支付、净成交、预估利润按账户授权同一批店相加。"""
    from app.services.advertiser_dashboard import _overview

    accounts = await _accounts_matching_advertiser_page(db, start, end, people)
    if allowed_ids is not None:
        accounts = [
            item
            for item in accounts
            if int(item.get("advertiser_id") or 0) in allowed_ids
        ]
    if not accounts:
        return None
    profit_map, _note = await shop_profits(db, start, end)
    profit_map = profit_map or {}
    buckets: dict[str, dict[str, Any]] = {}
    total_profit = 0.0
    for item in accounts:
        adv_id = int(item.get("advertiser_id") or 0)
        who = person_name(str(item.get("advertiser_name") or "")) or "未分组"
        profit = float(profit_map.get(adv_id, 0.0) or 0.0)
        total_profit += profit
        row = buckets.setdefault(who, {"name": who, "cost": 0.0, "profit": 0.0, "products": []})
        row["cost"] += float(item.get("stat_cost") or 0)
        row["profit"] += profit
    people_rows = [
        {
            "name": row["name"],
            "cost": round(row["cost"], 2),
            "profit": round(row["profit"], 2),
            "products": [],
        }
        for row in buckets.values()
    ]
    people_rows.sort(key=lambda row: row["cost"], reverse=True)
    return {
        "metrics": _overview(accounts),
        "profit": round(total_profit, 2),
        "people": people_rows,
    }


def plan_line_profit(plan: dict[str, Any]) -> float | None:
    """净成交 × (1 − 实际退款率) × 佣金 − 消耗。没有佣金或没有净成交则不计入。"""
    pct = product_report_service.commission_from_saved_or_name(
        plan.get("commission_pct"),
        str(plan.get("ad_name") or ""),
        str(plan.get("product_name") or ""),
    )
    return product_report_service.estimate_profit(
        plan.get("settle_amount"),
        plan.get("stat_cost"),
        plan.get("actual_refund_rate"),
        pct,
    )


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
        if plan.get("settle_amount") not in (None, ""):
            agg["settle"] = float(agg.get("settle") or 0) + float(plan.get("settle_amount") or 0)
            agg["settle_known"] = True
        if line is not None:
            agg["profit"] += line
            profit_sum += line
        agg["orders"] = int(agg.get("orders") or 0) + int(plan.get("order_count") or 0)
        if pid not in attached:
            attached.add(pid)
    products: list[dict[str, Any]] = []
    for pid, agg in bucket.items():
        products.append(
            {
                "pid": pid,
                "name": agg["name"] or product_report_service.display_name(pid, names.get(pid)),
                "cost": round(float(agg["cost"]), 2),
                "sales": int(sales_by_pid.get(pid) or agg.get("orders") or 0),
                "profit": round(float(agg["profit"]), 2),
                **({"settle": round(float(agg["settle"]), 2)} if agg.get("settle_known") else {}),
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
                **({"settle": round(float(val["settle"]), 2)} if "settle" in val else {}),
                **({"commission": val["commission"]} if val.get("commission") not in (None, "") else {}),
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
    official_total: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """用已经拉好的工作台账户和千川页面计划拼今天的总览。不再打接口。

    顶部消耗、支付、净成交用工作台。利润和销量用页面计划上的金额、订单数。
    传入商品报表时仍按商品行计算，15 分钟刷新不再传这份数据。
    """
    from app.services.plan_commission_sync import user_commissions_by_advertiser

    products_by_adv = products_by_adv or {}
    db_people = _person_advertisers(db)
    adv_for_meta = list(plans_by_adv)
    users = user_commissions_by_advertiser(db, adv_for_meta)
    meta = product_report_service.load_product_meta(db, adv_for_meta)
    board = overview_daily_service.rollup_people(workbench_accounts)
    official_rate = (official_total or {}).get("refund_rate_1h")
    if official_rate is None:
        peeked = take_roi2_total(today, today)
        official_rate = None if peeked is None else peeked.get("refund_rate_1h")
    all_row = board.get(overview_daily_service.ALL_PERSON)
    if official_rate is not None and isinstance(all_row, dict):
        all_row["refund_rate_1h"] = official_rate
    targets = sorted({key for key in board if key} | set(db_people))
    people_out: dict[str, Any] = {}
    accounts_out: dict[str, Any] = {}
    errors: list[str] = []
    for person in targets:
        adv_ids = db_people.get(person) or []
        failed = 0
        for adv_id in adv_ids:
            plans = plans_by_adv.get(int(adv_id))
            rows = products_by_adv.get(int(adv_id))
            if plans is None and not rows:
                failed += 1
                continue
            if rows:
                accounts_out[str(adv_id)] = account_from_product_rows(
                    rows,
                    plans or [],
                    meta,
                    int(adv_id),
                    users.get(int(adv_id), {}),
                )
            else:
                accounts_out[str(adv_id)] = account_from_plans(plans or [])
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


def _stored_product_profit(
    products: list[dict[str, Any]],
    plans: list[dict[str, Any]],
    meta_map: dict[tuple[int, int], dict[str, Any]],
    advertiser_id: int,
    user_by_ad: dict[int, float],
) -> float | None:
    """快照里已经写下净成交时，按商品重算。只有消耗、没有净成交时返回 None，沿用快照里算好的利润。"""
    usable = [
        item
        for item in products
        if item.get("settle") not in (None, "")
    ]
    if not usable:
        return None
    names = _commission_by_product(plans, user_by_ad)
    total = 0.0
    counted = False
    for item in usable:
        try:
            pid = int(item.get("pid") or 0)
        except (TypeError, ValueError):
            continue
        meta = meta_map.get((int(advertiser_id), pid))
        value = product_report_service.profit_of_product(
            settle=item.get("settle"),
            cost=item.get("cost"),
            meta=meta,
            product_name=str(item.get("name") or ""),
            user_commission=names.get(pid) or item.get("commission"),
        )
        if value is not None:
            counted = True
            total += value
    if not counted:
        return None
    return round(total, 2)


async def _apply_today_product_profits(
    db: Session,
    snapshot: dict[str, Any],
    rows: list[dict[str, Any]],
    people: list[str] | None,
    allowed_ids: set[int] | None = None,
) -> list[dict[str, Any]]:
    """今天的利润用快照里的商品净成交和消耗重算。计划缓存只补名称佣金，金额全是 0 时不用它。"""
    from app.services.plan_commission_sync import user_commissions_by_advertiser

    db_people = _people_scope(db, people, allowed_ids)
    adv_ids = [int(adv) for ids in db_people.values() for adv in ids]
    if not adv_ids:
        return rows
    accounts = snapshot.get("accounts") or {}
    cached = await today_plan_cache.mget_accounts(date.today().isoformat(), adv_ids)
    users = user_commissions_by_advertiser(db, adv_ids)
    meta = product_report_service.load_product_meta(db, adv_ids)
    profit_by_person: dict[str, float] = {}
    for person, ids in db_people.items():
        total = 0.0
        used = False
        for adv_id in ids:
            item = accounts.get(str(adv_id)) or {}
            plans = list((cached.get(int(adv_id)) or {}).get("plans") or [])
            part = _stored_product_profit(
                list(item.get("products") or []),
                plans,
                meta,
                int(adv_id),
                users.get(int(adv_id), {}),
            )
            if part is None:
                total += float(item.get("profit") or 0)
            else:
                used = True
                total += part
        if used or allowed_ids is not None:
            profit_by_person[person] = round(total, 2)
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for row in rows:
        name = str(row.get("name") or "")
        seen.add(name)
        if name in profit_by_person:
            out.append({**row, "profit": profit_by_person[name]})
        else:
            out.append(row)
    for name, profit in profit_by_person.items():
        if name in seen:
            continue
        out.append({"name": name, "cost": 0.0, "profit": profit})
    return out


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
    allowed_ids: set[int] | None = None,
) -> dict[str, Any]:
    """昨天及以前：消耗和商品都读日表。还没入库的日子先按 0，打开页面时不补拉。"""
    await set_progress(prog_key, status="running", current=0, total=1, message="正在读取账户历史数据")
    db_people = _people_scope(db, people, allowed_ids)
    adv_person = {adv_id: person for person, ids in db_people.items() for adv_id in ids}
    cost_by_advertiser = account_report_service.account_cost_by_advertiser(db, start, end)
    cost_by_person: dict[str, float] = defaultdict(float)
    for adv_id, cost in cost_by_advertiser.items():
        name = adv_person.get(int(adv_id))
        if not name:
            continue
        cost_by_person[name] += float(cost or 0)

    commission_map = product_report_service.product_commissions(db)
    adv_ids = [int(adv_id) for ids in db_people.values() for adv_id in ids]
    name_commission = await _plan_name_commissions(db, adv_ids)
    targets = sorted(set(cost_by_person) | set(db_people))
    gate = asyncio.Semaphore(1)
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
                None,
                person,
                db_people.get(person) or [],
                start,
                end,
                force=force,
                gate=gate,
                commission_map=commission_map,
                tick=tick,
                name_commission=name_commission,
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
        "name_commission": name_commission,
    }


async def build_overview(
    db: Session,
    start: str,
    end: str,
    people: list[str] | None,
    *,
    force: bool = False,
    allowed_ids: set[int] | None = None,
) -> dict[str, Any]:
    missing_days = await daily_sync.ensure_history_ready_for_overview(db, start, end)
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
            db, start, history_end, people, force=force, prog_key=prog_key, allowed_ids=allowed_ids
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
        today_rows = await _apply_today_product_profits(
            db, snapshot, today_rows, people, allowed_ids
        )
        if allowed_ids is not None:
            today_products = {}
            for adv_id in allowed_ids:
                _add_products(
                    today_products,
                    ((snapshot.get("accounts") or {}).get(str(adv_id)) or {}).get("products") or [],
                )
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
        note = "利润 = 净成交金额 × (1 − 实际退款率) × 佣金率 − 消耗。昨天及以前用数据库里的商品相加，今天用 Redis 里的商品相加。手填佣金优先，没有再用名称里的佣金。"
        from_cache = bool(history["from_cache"]) and today_cache
        cached_at = max(history["cached_at"], today_at)
    elif include_today:
        note = "今天的利润用 Redis 里各商品的净成交和消耗相加。手填的实际退款率和佣金优先，佣金没填再用名称，都没有的商品不计入。"
        from_cache = today_cache
        cached_at = today_at
    else:
        note = "" if missing_days else f"所选日期都已入库，数据截止 {end}。"
        from_cache = bool(history["from_cache"]) if history else False
        cached_at = history["cached_at"] if history else ""
    if missing_days:
        shown = "、".join(missing_days[:8])
        extra = f" 等 {len(missing_days)} 天" if len(missing_days) > 8 else ""
        note = f"{note} {shown}{extra} 还没有入库，这几天先不计入。".strip()
    await set_progress(prog_key, status="done", current=1, total=1, message="完成")
    today_part = _workbench_slice(snapshot, people) if include_today and snapshot is not None else None
    if allowed_ids is not None and include_today:
        live_accounts = await load_roi2_accounts(today, today)
        chosen = [
            item
            for item in live_accounts
            if int(item.get("advertiser_id") or 0) in allowed_ids
        ]
        bucket = overview_daily_service._blank()
        for item in chosen:
            overview_daily_service._add(bucket, item)
        today_part = overview_daily_service._finish(bucket, chosen)
        if len(today_rows) == 1:
            today_rows[0]["cost"] = today_part["stat_cost"]
    trend = None
    trend_person = None
    if people and len(people) == 1 and start < end:
        trend_person = people[0]
        trend = _person_trend(
            db,
            trend_person,
            start,
            end,
            today_rows if include_today else None,
            history.get("name_commission") if history else None,
            allowed_ids,
        )
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
    result["daily_trend"] = _daily_trend(
        db,
        result["start_date"],
        result["end_date"],
        people,
        today_rows if include_today else None,
        today_part,
        history.get("name_commission") if history else None,
        allowed_ids,
    )
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
    if allowed_ids is not None:
        money = {}
        if has_history:
            money = account_report_service.account_money_by_day(
                db, list(allowed_ids), start, history_end
            )
        cost = sum(float(item["cost"]) for item in money.values())
        pay = sum(float(item["pay_amount"]) for item in money.values())
        settle = sum(float(item["settle_amount"]) for item in money.values())
        if include_today and today_part:
            cost += float(today_part.get("stat_cost") or 0)
            pay += float(today_part.get("pay_amount") or 0)
            settle += float(today_part.get("settle_amount") or 0)
        metrics["stat_cost"] = round(cost, 2)
        metrics["pay_amount"] = round(pay, 2)
        metrics["settle_amount"] = round(settle, 2)
        metrics["pay_roi"] = round(pay / cost, 4) if cost > 0 else None
        metrics["settle_roi"] = round(settle / cost, 4) if cost > 0 else None
    result["workbench"] = overview_daily_service.with_profit_rate(metrics, result["kpis"]["total_profit"])
    result["kpis"]["total_cost"] = metrics["stat_cost"]
    try:
        aligned = await align_with_advertiser_page(
            db, result["start_date"], result["end_date"], people, allowed_ids
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("按账户授权对齐总览失败：%s", exc)
        aligned = None
    if aligned:
        board = dict(aligned["metrics"])
        if metrics.get("refund_rate_1h") is not None:
            board["refund_rate_1h"] = metrics["refund_rate_1h"]
        result["people"] = aligned["people"]
        result["kpis"]["total_profit"] = aligned["profit"]
        result["kpis"]["total_cost"] = board["stat_cost"]
        result["kpis"]["people_count"] = len(aligned["people"])
        result["workbench"] = overview_daily_service.with_profit_rate(board, aligned["profit"])
        today_iso = date.today().isoformat()
        if result["start_date"] <= today_iso <= result["end_date"]:
            today_aligned = aligned
            if not (result["start_date"] == result["end_date"] == today_iso):
                try:
                    today_aligned = await align_with_advertiser_page(
                        db, today_iso, today_iso, people, allowed_ids
                    )
                except Exception as exc:  # noqa: BLE001
                    logger.warning("按账户授权对齐今天趋势失败：%s", exc)
                    today_aligned = None
            if today_aligned:
                for point in result.get("daily_trend") or []:
                    if point.get("date") != today_iso:
                        continue
                    point["cost"] = today_aligned["metrics"]["stat_cost"]
                    point["pay_amount"] = today_aligned["metrics"]["pay_amount"]
                    point["settle_amount"] = today_aligned["metrics"]["settle_amount"]
                    point["profit"] = today_aligned["profit"]
                    pay = float(point["pay_amount"] or 0)
                    point["profit_rate"] = round(today_aligned["profit"] / pay, 6) if pay > 0 else None
    return result