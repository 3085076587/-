"""商品维度报表：开放平台商品接口拉数 + 入库，总览优先读库。

接口 /v1.0/qianchuan/report/uni_promotion/data/get/，
data_topic=SITE_PROMOTION_PRODUCT_PRODUCT：单账户一次请求返回全部商品，
不用再拉计划列表分页筛选。历史区间靠 qianchuan_product_daily 直接算。
"""

from __future__ import annotations

import logging
from collections import defaultdict
from datetime import date, datetime, timedelta
from typing import Any

from sqlalchemy import func, select, text
from sqlalchemy.dialects.mysql import insert as mysql_insert
from sqlalchemy.orm import Session

from app.clients.oceanengine import OceanEngineApiError, OceanEngineClient
from app.models.qianchuan_account_daily import QianchuanAccountDaily
from app.models.qianchuan_product import QianchuanProduct
from app.models.qianchuan_product_daily import QianchuanProductDaily, QianchuanProductSync
from app.services.uni_plan_service import DEFAULT_COMMISSION_PCT, _num

logger = logging.getLogger("uvicorn.error")

# 与计划页利润公式一致的退款率附加项。
REFUND_EXTRA = 0.05

# 商品维度指标（data/get 的 metrics）。金额单位是元。
# 接口不认订单数/净成交字段时，自动退回核心三项。
PRODUCT_FIELDS = [
    "stat_cost",
    "total_pay_order_gmv_for_roi2",
    "total_prepay_and_pay_order_roi2",
    "total_pay_order_count_for_roi2",
    "total_order_settle_amount_for_roi2_1h",
    "total_prepay_and_pay_settle_roi2_1h",
    "total_refund_order_gmv_for_roi2_1h_rate",
]
CORE_FIELDS = [
    "stat_cost",
    "total_pay_order_gmv_for_roi2",
    "total_prepay_and_pay_order_roi2",
]


def _cell_value(cell: Any) -> Any:
    """data/get 每个维度/指标都是 {"Value":..,"ValueStr":..}，取原值。"""
    if isinstance(cell, dict):
        return cell.get("Value")
    return cell


def _norm_rate(value: Any) -> float | None:
    """退款率可能是 0~1 小数或百分数（如 10.21），统一折到 0~1。"""
    num = _num(value, default=-1.0)
    if num < 0:
        return None
    if num > 1:
        num = num / 100.0
    return round(min(1.0, max(0.0, num)), 6)


def _day_key(value: Any) -> str:
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    return str(value)[:10]


def account_refund_rates(
    db: Session,
    start: str,
    end: str,
    advertiser_ids: list[int] | None = None,
) -> dict[tuple[int, str], float]:
    """账户某一天的 1 小时退货率。商品行自己没有退货率时用这个。"""
    stmt = select(
        QianchuanAccountDaily.advertiser_id,
        QianchuanAccountDaily.stat_date,
        QianchuanAccountDaily.refund_rate_1h,
    ).where(
        QianchuanAccountDaily.stat_date >= start,
        QianchuanAccountDaily.stat_date <= end,
        QianchuanAccountDaily.refund_rate_1h.is_not(None),
    )
    if advertiser_ids:
        stmt = stmt.where(
            QianchuanAccountDaily.advertiser_id.in_([int(adv_id) for adv_id in advertiser_ids])
        )
    found: dict[tuple[int, str], float] = {}
    for adv_id, stat_date, rate in db.execute(stmt).all():
        found[(int(adv_id), _day_key(stat_date))] = float(rate)
    return found


def refund_for_product_row(
    product_rate: Any,
    advertiser_id: int,
    stat_date: Any,
    account_rates: dict[tuple[int, str], float],
) -> float | None:
    """商品自己有 1 小时退货率就用商品的，没有就用这个账户当天的。"""
    if product_rate is not None:
        return float(product_rate)
    return account_rates.get((int(advertiser_id), _day_key(stat_date)))


def display_name(product_id: int, name: str | None) -> str:
    """有真名用真名；拿不到名（计划已不存在）时用商品ID后6位兜底，避免整列「无数据」。"""
    if name:
        return name
    pid = str(product_id)
    return f"商品…{pid[-6:]}"


def profit_basis(
    cost: float,
    pay_amount: float,
    pay_roi: float | None,
    refund_rate_1h: float | None,
) -> tuple[float, float] | None:
    """利润里跟佣金无关的部分。退款率只用接口给的 1 小时退货率，再加 5%。"""
    if cost <= 0 and not pay_amount:
        return None
    roi = pay_roi
    if roi is None and cost > 0 and pay_amount:
        roi = pay_amount / cost
    rate = _norm_rate(refund_rate_1h)
    if roi is None or rate is None:
        return None
    keep = 1.0 - min(1.0, rate + REFUND_EXTRA)
    return float(cost) * float(roi) * keep, float(cost)


def product_profit(
    cost: float,
    pay_amount: float,
    pay_roi: float | None,
    refund_rate_1h: float | None,
    commission_pct: float,
) -> float | None:
    """商品口径利润。退款率用已入库的 1 小时退货率，再加 5%。"""
    parts = profit_basis(cost, pay_amount, pay_roi, refund_rate_1h)
    if parts is None:
        return None
    basis, used_cost = parts
    return basis * (float(commission_pct) / 100.0) - used_cost


def resolve_commission_pct(commission_map: dict[int, float], product_id: int) -> float:
    """库里该商品佣金为空或 0 时按 60%。"""
    pct = float(commission_map.get(int(product_id), 0) or 0)
    return pct if pct > 0 else DEFAULT_COMMISSION_PCT


def rollup_today_rows(
    rows: list[dict[str, Any]], commission_map: dict[int, float]
) -> dict[int, dict[str, Any]]:
    """今天的商品行合成一份，带上佣金和重算利润要用的底数。不打接口。"""
    bucket: dict[int, dict[str, Any]] = {}
    for row in rows:
        pid = int(row["product_id"])
        agg = bucket.setdefault(
            pid,
            {
                "name": display_name(pid, row.get("product_name")),
                "cost": 0.0,
                "sales": 0,
                "basis": 0.0,
                "profit_cost": 0.0,
            },
        )
        if row.get("product_name"):
            agg["name"] = display_name(pid, row.get("product_name"))
        agg["cost"] += float(row.get("cost") or 0)
        agg["sales"] += int(row.get("order_count") or 0)
        parts = profit_basis(
            float(row.get("cost") or 0),
            float(row.get("pay_amount") or 0),
            row.get("pay_roi"),
            row.get("refund_rate_1h"),
        )
        if parts is not None:
            basis, used_cost = parts
            agg["basis"] += basis
            agg["profit_cost"] += used_cost
    out: dict[int, dict[str, Any]] = {}
    for pid, agg in bucket.items():
        pct = resolve_commission_pct(commission_map, pid)
        out[pid] = {
            "name": agg["name"],
            "cost": round(agg["cost"], 2),
            "sales": int(agg["sales"]),
            "basis": agg["basis"],
            "profit_cost": agg["profit_cost"],
            "commission": pct,
            "profit": round(agg["basis"] * pct / 100.0 - agg["profit_cost"], 2),
        }
    return out


async def fetch_products(
    client: OceanEngineClient, advertiser_id: int, start: str, end: str
) -> list[dict[str, Any]]:
    """拉单账户区间内商品维度数据，金额已换算成元，并用计划列表补齐商品标题。"""
    try:
        rows = await _fetch_paged(client, advertiser_id, start, end, PRODUCT_FIELDS)
    except OceanEngineApiError as exc:
        logger.warning("商品报表全指标拉取失败，退回核心指标重试：%s", exc)
        rows = await _fetch_paged(client, advertiser_id, start, end, CORE_FIELDS)
    if rows:
        need_ids = {r["product_id"] for r in rows}
        names = await fetch_product_names(
            client, advertiser_id, start, end, need_ids
        )
        # 计划列表查不到的（计划已删/非全域），改用可投商品列表按 product_id 反查
        await _fill_names_from_available(client, advertiser_id, need_ids, names)
        for r in rows:
            r["product_name"] = names.get(r["product_id"], "")
    return rows


async def _fill_names_from_available(
    client: OceanEngineClient,
    advertiser_id: int,
    need_ids: set[int],
    names: dict[int, str],
) -> None:
    """对 names 里仍缺的商品，用 /product/available/get/ 按 product_id 反查补名（原地写回 names）。"""
    missing = sorted(p for p in need_ids if not names.get(p))
    if not missing:
        return
    try:
        extra = await client.get_available_products(advertiser_id, missing)
    except OceanEngineApiError as exc:
        logger.warning("可投商品列表补名失败 advertiser=%s：%s", advertiser_id, exc)
        return
    for pid, name in extra.items():
        if name and not names.get(pid):
            names[pid] = name


async def fetch_product_names(
    client: OceanEngineClient,
    advertiser_id: int,
    start: str,
    end: str,
    need_ids: set[int],
) -> dict[int, str]:
    """从全域计划列表 product_info 建 商品ID→标题 映射。

    data/get 商品维度不返回名称，只能借计划里的 product_info 补。
    先查短视频带货，仍有未命中的商品再查直播带货，少打一次接口。
    """
    names: dict[int, str] = {}
    for goal in ("VIDEO_PROM_GOODS", "LIVE_PROM_GOODS"):
        if need_ids and need_ids.issubset(names):
            break
        page = 1
        while True:
            try:
                data = await client.list_uni_promotion(
                    advertiser_id,
                    f"{start} 00:00:00",
                    f"{end} 23:59:59",
                    marketing_goal=goal,
                    fields=["stat_cost"],
                    page=page,
                    page_size=100,
                )
            except OceanEngineApiError as exc:
                logger.warning("拉商品名映射失败 advertiser=%s goal=%s：%s", advertiser_id, goal, exc)
                break
            for ad in data.get("ad_list") or []:
                for p in ad.get("product_info") or []:
                    pid = p.get("product_id")
                    name = str(p.get("product_name") or "")
                    if pid and name:
                        names[int(pid)] = name
            page_info = data.get("page_info") or {}
            batch = data.get("ad_list") or []
            total_page = int(page_info.get("total_page") or 0)
            has_more = page < total_page if total_page else len(batch) >= 100
            if not batch or not has_more:
                break
            page += 1
    return names


async def repair_product_names(
    db: Session,
    client: OceanEngineClient,
    *,
    lookback_days: int = 90,
    advertiser_ids: list[int] | None = None,
    progress_cb: Any = None,
) -> int:
    """补齐主档里 product_name 为空的商品。

    按账户用全域计划列表（fetch_product_names）在大时间窗内查名，
    只更新名字仍为空的行。返回实际回填条数。progress_cb(done,total,adv) 可选。
    """
    stmt = select(QianchuanProduct.advertiser_id, QianchuanProduct.product_id).where(
        QianchuanProduct.product_name == ""
    )
    if advertiser_ids:
        stmt = stmt.where(QianchuanProduct.advertiser_id.in_(advertiser_ids))
    rows = db.execute(stmt).all()
    if not rows:
        return 0

    by_adv: dict[int, set[int]] = {}
    for adv, pid in rows:
        by_adv.setdefault(int(adv), set()).add(int(pid))

    end = date.today().isoformat()
    start = (date.today() - timedelta(days=lookback_days)).isoformat()
    total = len(by_adv)
    updated = 0
    for idx, (adv, pids) in enumerate(by_adv.items(), start=1):
        if progress_cb is not None:
            progress_cb(idx - 1, total, adv)
        try:
            names = await fetch_product_names(client, adv, start, end, pids)
        except OceanEngineApiError as exc:
            logger.warning("补名查计划失败 账户%s：%s", adv, exc)
            names = {}
        # 计划列表仍查不到的，用可投商品列表按 product_id 反查兜底
        await _fill_names_from_available(client, adv, pids, names)
        for pid, name in names.items():
            if pid in pids and name:
                db.query(QianchuanProduct).filter(
                    QianchuanProduct.advertiser_id == adv,
                    QianchuanProduct.product_id == pid,
                ).update({QianchuanProduct.product_name: name}, synchronize_session=False)
                updated += 1
        db.commit()
    if progress_cb is not None:
        progress_cb(total, total, 0)
    return updated


async def _fetch_paged(
    client: OceanEngineClient,
    advertiser_id: int,
    start: str,
    end: str,
    metrics: list[str],
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    page = 1
    while True:
        data = await client.get_uni_promotion_product_data(
            advertiser_id, start, end, metrics=metrics, page=page, page_size=100
        )
        batch = data.get("rows") or []
        for item in batch:
            if not isinstance(item, dict):
                continue
            dims = item.get("dimensions") or {}
            mets = item.get("metrics") or {}
            raw_pid = _cell_value(dims.get("qianchuan_product_id"))
            if raw_pid in (None, ""):
                continue
            settle_cell = _cell_value(mets.get("total_order_settle_amount_for_roi2_1h"))
            settle_roi_cell = _cell_value(mets.get("total_prepay_and_pay_settle_roi2_1h"))
            rows.append(
                {
                    "product_id": int(raw_pid),
                    # data/get 的商品维度不返回商品名，名称由计划列表补齐。
                    "product_name": "",
                    "cost": _num(_cell_value(mets.get("stat_cost"))),
                    "pay_amount": _num(_cell_value(mets.get("total_pay_order_gmv_for_roi2"))),
                    "pay_roi": _num(_cell_value(mets.get("total_prepay_and_pay_order_roi2"))) or None,
                    "settle_amount": (
                        _num(settle_cell) if settle_cell not in (None, "") else None
                    ),
                    "settle_roi": (
                        _num(settle_roi_cell) if settle_roi_cell not in (None, "") else None
                    ),
                    "refund_rate_1h": _norm_rate(
                        _cell_value(mets.get("total_refund_order_gmv_for_roi2_1h_rate"))
                    ),
                    "order_count": int(_num(_cell_value(mets.get("total_pay_order_count_for_roi2")))),
                }
            )
        page_info = data.get("page_info") or {}
        total_page = int(page_info.get("total_page") or 0)
        if total_page:
            has_more = page < total_page
        else:
            has_more = len(batch) >= 100
        if not batch or not has_more:
            break
        page += 1
    return rows


def upsert_products(
    db: Session,
    advertiser_id: int,
    rows: list[dict[str, Any]],
    *,
    stat_date: date | None = None,
) -> None:
    """写商品主档；带 stat_date 时同步覆盖当日商品日数据。"""
    if not rows:
        return
    for row in rows:
        stmt = mysql_insert(QianchuanProduct).values(
            advertiser_id=advertiser_id,
            product_id=row["product_id"],
            product_name=row["product_name"],
        )
        # data/get 不返回商品名，空名不能覆盖主档已有标题；佣金是用户配置，也不覆盖。
        if row["product_name"]:
            stmt = stmt.on_duplicate_key_update(product_name=stmt.inserted.product_name)
        else:
            stmt = stmt.on_duplicate_key_update(id=QianchuanProduct.id)
        db.execute(stmt)
        if stat_date is not None:
            daily = mysql_insert(QianchuanProductDaily).values(
                advertiser_id=advertiser_id,
                product_id=row["product_id"],
                stat_date=stat_date,
                cost=row["cost"],
                pay_amount=row["pay_amount"],
                pay_roi=row["pay_roi"],
                settle_amount=row["settle_amount"],
                settle_roi=row.get("settle_roi"),
                refund_rate_1h=row.get("refund_rate_1h"),
                order_count=row["order_count"],
            )
            daily = daily.on_duplicate_key_update(
                cost=daily.inserted.cost,
                pay_amount=daily.inserted.pay_amount,
                pay_roi=daily.inserted.pay_roi,
                settle_amount=daily.inserted.settle_amount,
                settle_roi=daily.inserted.settle_roi,
                refund_rate_1h=daily.inserted.refund_rate_1h,
                order_count=daily.inserted.order_count,
            )
            db.execute(daily)
    db.commit()


async def sync_day(
    client: OceanEngineClient, db: Session, advertiser_id: int, day: str
) -> int:
    """同步单账户单日商品数据入库，返回行数。定时任务调用。总写同步标记。"""
    rows = await fetch_products(client, advertiser_id, day, day)
    upsert_products(db, advertiser_id, rows, stat_date=date.fromisoformat(day))
    mark_product_synced(db, advertiser_id, day, len(rows))
    return len(rows)


def product_commissions(db: Session) -> dict[int, float]:
    """商品维度佣金（同一商品多账户共用），未配置默认 60%。"""
    out: dict[int, float] = {}
    for row in db.scalars(select(QianchuanProduct)).all():
        pct = float(row.commission_pct or 0)
        if pct > 0:
            out[int(row.product_id)] = pct
    return out


def set_product_commission(db: Session, product_id: int, commission_pct: float) -> int:
    """更新商品佣金，返回命中行数。"""
    rows = db.scalars(
        select(QianchuanProduct).where(QianchuanProduct.product_id == product_id)
    ).all()
    for row in rows:
        row.commission_pct = commission_pct
    db.commit()
    return len(rows)


def _range_days(start: str, end: str) -> list[str]:
    """闭区间 [start, end] 的每一天（yyyy-MM-dd）。"""
    d0 = date.fromisoformat(start)
    d1 = date.fromisoformat(end)
    days: list[str] = []
    while d0 <= d1:
        days.append(d0.isoformat())
        d0 += timedelta(days=1)
    return days


def _as_date(day: str | date) -> date:
    return date.fromisoformat(day) if isinstance(day, str) else day


def mark_product_synced(db: Session, advertiser_id: int, day: str | date, count: int) -> None:
    """写/更新同步标记：表示该账户该天的商品数据已同步（count 可为 0）。"""
    stmt = mysql_insert(QianchuanProductSync).values(
        advertiser_id=advertiser_id,
        stat_date=_as_date(day),
        product_count=int(count),
    )
    stmt = stmt.on_duplicate_key_update(product_count=stmt.inserted.product_count)
    db.execute(stmt)
    db.commit()


def has_product_sync_marker(db: Session, advertiser_id: int, day: str | date) -> bool:
    c = db.scalar(
        select(func.count())
        .select_from(QianchuanProductSync)
        .where(
            QianchuanProductSync.advertiser_id == advertiser_id,
            QianchuanProductSync.stat_date == _as_date(day),
        )
    )
    return int(c or 0) > 0


def product_row_count(db: Session, advertiser_id: int, day: str | date) -> int:
    return int(
        db.scalar(
            select(func.count())
            .select_from(QianchuanProductDaily)
            .where(
                QianchuanProductDaily.advertiser_id == advertiser_id,
                QianchuanProductDaily.stat_date == _as_date(day),
            )
        )
        or 0
    )


def _account_has_cost(db: Session, advertiser_id: int, day: str | date) -> bool:
    """该账户当天工作台是否有消耗（无消耗→不可能有商品行，直接标记为空）。"""
    cost = db.scalar(
        select(func.sum(QianchuanAccountDaily.cost)).where(
            QianchuanAccountDaily.advertiser_id == advertiser_id,
            QianchuanAccountDaily.stat_date == _as_date(day),
        )
    )
    return float(cost or 0) > 0


async def ensure_product_days(
    db: Session,
    client: OceanEngineClient,
    advertiser_id: int,
    days: list[str],
    gate: Any,
) -> int:
    """尽量不打接口地补齐商品同步标记，返回实际打接口（sync_day）次数。

    逐日：已有标记→跳过；已有商品行但无标记→直接补标记（零接口）；
    当天零消耗→标记为空（零接口）；否则才 sync_day 拉接口。gate 为 asyncio.Semaphore。
    """
    pulled = 0
    for day in days:
        if has_product_sync_marker(db, advertiser_id, day):
            continue
        cnt = product_row_count(db, advertiser_id, day)
        if cnt:
            mark_product_synced(db, advertiser_id, day, cnt)
            continue
        if not _account_has_cost(db, advertiser_id, day):
            mark_product_synced(db, advertiser_id, day, 0)
            continue
        async with gate:
            try:
                await sync_day(client, db, advertiser_id, day)  # 内部已写标记
                pulled += 1
            except Exception as exc:  # noqa: BLE001
                logger.warning("自愈商品日数据失败 账户%s %s：%s", advertiser_id, day, exc)
    return pulled


def seed_markers_from_daily(db: Session) -> int:
    """把已入库的商品日数据补写同步标记（幂等、零接口）。

    用于历史数据首次接入标记表：已有日行→自动视为已同步，重启后秒变覆盖。
    """
    res = db.execute(
        text(
            "INSERT IGNORE INTO qianchuan_product_sync "
            "(advertiser_id, stat_date, product_count) "
            "SELECT advertiser_id, stat_date, COUNT(*) "
            "FROM qianchuan_product_daily GROUP BY advertiser_id, stat_date"
        )
    )
    db.commit()
    return int(res.rowcount or 0)


def _covered(db: Session, advertiser_id: int, start: str, end: str) -> bool:
    """区间内每天都有同步标记才算覆盖（零商品日也已标记）。"""
    days = _range_days(start, end)
    if not days:
        return False
    have = db.scalar(
        select(func.count(func.distinct(QianchuanProductSync.stat_date))).where(
            QianchuanProductSync.advertiser_id == advertiser_id,
            QianchuanProductSync.stat_date >= start,
            QianchuanProductSync.stat_date <= end,
        )
    )
    return int(have or 0) >= len(days)


def range_fully_synced(
    db: Session, advertiser_ids: list[int], start: str, end: str
) -> bool:
    """这些账户在区间内是否每天都有商品同步标记（含零消耗空标记）。"""
    if not advertiser_ids:
        return True
    for adv_id in advertiser_ids:
        if not _covered(db, int(adv_id), start, end):
            return False
    return True


def person_aggregate_from_db(
    db: Session,
    advertiser_ids: list[int],
    start: str,
    end: str,
    commission_map: dict[int, float],
) -> dict[str, Any] | None:
    """多人多账户区间利润：日表齐全时一次读库聚合，不打接口。"""
    if not range_fully_synced(db, advertiser_ids, start, end):
        return None
    profit = 0.0
    plan_cost = 0.0
    plan_count = 0
    products: dict[int, dict[str, Any]] = {}
    for adv_id in advertiser_ids:
        block = stats_from_db(db, int(adv_id), start, end, commission_map)
        if block is None:
            return None
        profit += float(block["profit"])
        plan_cost += float(block["plan_cost"])
        plan_count += int(block["plan_count"])
        for pid, val in block["products"].items():
            agg = products.setdefault(
                int(pid),
                {"name": val.get("name") or "无数据", "cost": 0.0, "sales": 0},
            )
            agg["name"] = val.get("name") or agg["name"]
            agg["cost"] += float(val.get("cost") or 0)
            agg["sales"] += int(val.get("sales") or 0)
    return {
        "profit": round(profit, 2),
        "plan_cost": round(plan_cost, 2),
        "plan_count": plan_count,
        "products": [
            {"pid": pid, "name": v["name"], "cost": round(v["cost"], 2), "sales": v["sales"]}
            for pid, v in products.items()
        ],
        "failed_advertisers": 0,
        "from_cache": False,
        "source": "db_batch",
    }


def finalize_product_markers_for_day(db: Session, day: str, advertiser_ids: list[int]) -> int:
    """补写零消耗/已有商品行但缺标记的账户，返回新补标记数。"""
    added = 0
    for adv_id in advertiser_ids:
        if has_product_sync_marker(db, adv_id, day):
            continue
        cnt = product_row_count(db, adv_id, day)
        if cnt:
            mark_product_synced(db, adv_id, day, cnt)
            added += 1
            continue
        if not _account_has_cost(db, adv_id, day):
            mark_product_synced(db, adv_id, day, 0)
            added += 1
    return added


def advertisers_missing_product_marker(db: Session, day: str, advertiser_ids: list[int]) -> list[int]:
    """指定日期仍无商品同步标记的账户。"""
    missing: list[int] = []
    for adv_id in advertiser_ids:
        if not has_product_sync_marker(db, adv_id, day):
            missing.append(int(adv_id))
    return missing


def missing_days(db: Session, advertiser_id: int, start: str, end: str) -> list[str]:
    """区间内该账户尚无同步标记的日期（真正需要同步/补标记的）。"""
    have = {
        d.isoformat() if isinstance(d, date) else str(d)
        for (d,) in db.execute(
            select(QianchuanProductSync.stat_date).where(
                QianchuanProductSync.advertiser_id == advertiser_id,
                QianchuanProductSync.stat_date >= start,
                QianchuanProductSync.stat_date <= end,
            )
        ).all()
    }
    return [day for day in _range_days(start, end) if day not in have]


def stats_from_db(
    db: Session,
    advertiser_id: int,
    start: str,
    end: str,
    commission_map: dict[int, float],
) -> dict[str, Any] | None:
    """历史区间日数据齐全时直接 SQL 聚合，零接口调用。未覆盖返回 None。"""
    if not _covered(db, advertiser_id, start, end):
        return None
    account_rates = account_refund_rates(db, start, end, [advertiser_id])
    rows = db.execute(
        select(
            QianchuanProductDaily.stat_date,
            QianchuanProductDaily.product_id,
            QianchuanProductDaily.cost,
            QianchuanProductDaily.pay_amount,
            QianchuanProductDaily.pay_roi,
            QianchuanProductDaily.refund_rate_1h,
            QianchuanProductDaily.order_count,
        ).where(
            QianchuanProductDaily.advertiser_id == advertiser_id,
            QianchuanProductDaily.stat_date >= start,
            QianchuanProductDaily.stat_date <= end,
        )
    ).all()
    names = {
        int(p.product_id): p.product_name
        for p in db.scalars(
            select(QianchuanProduct).where(QianchuanProduct.advertiser_id == advertiser_id)
        ).all()
    }
    products: dict[int, dict[str, Any]] = {}
    profit = 0.0
    cost_total = 0.0
    for stat_date, pid, cost, pay, roi, refund_rate, orders in rows:
        cost = float(cost or 0)
        pay = float(pay or 0)
        roi_v = float(roi) if roi is not None else None
        refund_v = refund_for_product_row(refund_rate, advertiser_id, stat_date, account_rates)
        pct = commission_map.get(int(pid), DEFAULT_COMMISSION_PCT)
        value = product_profit(cost, pay, roi_v, refund_v, pct)
        if value is not None:
            profit += value
        cost_total += cost
        agg = products.setdefault(
            int(pid),
            {
                "name": display_name(int(pid), names.get(int(pid))),
                "cost": 0.0,
                "sales": 0,
            },
        )
        agg["cost"] += cost
        agg["sales"] += int(orders or 0)
    for item in products.values():
        item["cost"] = round(item["cost"], 2)
    return {
        "profit": round(profit, 2),
        "plan_cost": round(cost_total, 2),
        "plan_count": len(products),
        "products": products,
        "source": "db",
    }


def aggregate_products(
    rows: list[dict[str, Any]],
    commission_map: dict[int, float],
) -> dict[str, Any]:
    """接口返回的商品行聚合：利润 + 消耗 + 按商品合并。"""
    profit = 0.0
    cost_total = 0.0
    products: dict[int, dict[str, Any]] = {}
    for row in rows:
        pid = int(row["product_id"])
        pct = commission_map.get(pid, DEFAULT_COMMISSION_PCT)
        value = product_profit(
            float(row["cost"]),
            float(row["pay_amount"]),
            row["pay_roi"],
            row.get("refund_rate_1h"),
            pct,
        )
        if value is not None:
            profit += value
        cost_total += float(row["cost"])
        agg = products.setdefault(
            pid, {"name": display_name(pid, row["product_name"]), "cost": 0.0, "sales": 0}
        )
        agg["cost"] += float(row["cost"])
        agg["sales"] += int(row["order_count"])
    return {
        "profit": round(profit, 2),
        "plan_cost": round(cost_total, 2),
        "plan_count": len(products),
        "products": {
            pid: {"name": v["name"], "cost": round(v["cost"], 2), "sales": v["sales"]}
            for pid, v in products.items()
        },
        "source": "api",
    }


def profit_by_day(
    db: Session,
    advertiser_ids: list[int],
    start: str,
    end: str,
    commission_map: dict[int, float] | None = None,
) -> dict[str, float]:
    """这些账户每一天的商品利润。按当天每一行单独计算再相加，不把多天揉成一个 ROI。"""
    if not advertiser_ids:
        return {}
    commissions = product_commissions(db) if commission_map is None else commission_map
    account_rates = account_refund_rates(db, start, end, advertiser_ids)
    rows = db.execute(
        select(
            QianchuanProductDaily.stat_date,
            QianchuanProductDaily.advertiser_id,
            QianchuanProductDaily.product_id,
            QianchuanProductDaily.cost,
            QianchuanProductDaily.pay_amount,
            QianchuanProductDaily.pay_roi,
            QianchuanProductDaily.refund_rate_1h,
        ).where(
            QianchuanProductDaily.advertiser_id.in_(advertiser_ids),
            QianchuanProductDaily.stat_date >= start,
            QianchuanProductDaily.stat_date <= end,
        )
    ).all()
    bag: dict[str, float] = defaultdict(float)
    for stat_date, adv_id, pid, cost, pay, roi, refund_rate in rows:
        cost_f = float(cost or 0)
        pay_f = float(pay or 0)
        roi_v = float(roi) if roi is not None else None
        refund_v = refund_for_product_row(refund_rate, int(adv_id), stat_date, account_rates)
        value = product_profit(
            cost_f,
            pay_f,
            roi_v,
            refund_v,
            resolve_commission_pct(commissions, int(pid)),
        )
        if value is None:
            continue
        key = stat_date.isoformat() if isinstance(stat_date, date) else str(stat_date)[:10]
        bag[key] += value
    return {day: round(total, 2) for day, total in bag.items()}


def profits_by_advertiser(db: Session, start: str, end: str) -> dict[int, float]:
    """按店铺把区间内各商品利润加总。只读日表，不打接口。佣金空或 0 按 60%。"""
    commission_map = product_commissions(db)
    account_rates = account_refund_rates(db, start, end)
    rows = db.execute(
        select(
            QianchuanProductDaily.advertiser_id,
            QianchuanProductDaily.stat_date,
            QianchuanProductDaily.product_id,
            QianchuanProductDaily.cost,
            QianchuanProductDaily.pay_amount,
            QianchuanProductDaily.pay_roi,
            QianchuanProductDaily.refund_rate_1h,
        ).where(
            QianchuanProductDaily.stat_date >= start,
            QianchuanProductDaily.stat_date <= end,
        )
    ).all()
    bag: dict[int, float] = {}
    for adv, stat_date, pid, cost, pay, roi, refund_rate in rows:
        cost_f = float(cost or 0)
        pay_f = float(pay or 0)
        roi_v = float(roi) if roi is not None else None
        refund_v = refund_for_product_row(refund_rate, int(adv), stat_date, account_rates)
        value = product_profit(
            cost_f,
            pay_f,
            roi_v,
            refund_v,
            resolve_commission_pct(commission_map, int(pid)),
        )
        if value is None:
            continue
        adv_id = int(adv)
        bag[adv_id] = bag.get(adv_id, 0.0) + value
    return {adv: round(val, 2) for adv, val in bag.items()}
