"""数据总览的工作台日汇总。

今天直接拉工作台账户表，加总方式和子账户页顶部卡片相同。
昨天及更早读 qianchuan_overview_daily，由每天 00:01 的入库任务写入。
"""

from __future__ import annotations

import json
import logging
from collections import defaultdict
from datetime import date, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.dialects.mysql import insert as mysql_insert
from sqlalchemy.orm import Session

from app.models.advertiser import QianchuanAdvertiser
from app.models.qianchuan_account_daily import QianchuanAccountDaily
from app.models.qianchuan_overview_daily import QianchuanOverviewDaily
from app.core.redis_client import get_cache_redis
from app.services.advertiser_dashboard import (
    _has_parentheses,
    fetch_roi2_total,
    load_roi2_accounts,
    person_name,
    take_roi2_total,
    weighted_pay_roi,
    weighted_refund_rate,
    weighted_settle_roi,
)

logger = logging.getLogger("uvicorn.error")
_REFUND_CACHE_SECONDS = 15 * 60

ALL_PERSON = ""


def _blank() -> dict[str, float | int]:
    return {"stat_cost": 0.0, "pay_amount": 0.0, "settle_amount": 0.0}


def _add(bucket: dict[str, float | int], item: dict[str, Any]) -> None:
    bucket["stat_cost"] = float(bucket["stat_cost"]) + float(item.get("stat_cost") or 0)
    bucket["pay_amount"] = float(bucket["pay_amount"]) + float(item.get("pay_amount") or 0)
    bucket["settle_amount"] = float(bucket["settle_amount"]) + float(item.get("settle_amount") or 0)


def _finish(bucket: dict[str, float | int], items: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    cost = round(float(bucket["stat_cost"]), 2)
    pay = round(float(bucket["pay_amount"]), 2)
    settle = round(float(bucket["settle_amount"]), 2)
    roi_items = items or []
    return {
        "stat_cost": cost,
        "pay_amount": pay,
        "settle_amount": settle,
        "pay_roi": weighted_pay_roi(roi_items) if roi_items else None,
        "settle_roi": weighted_settle_roi(roi_items) if roi_items else None,
        "refund_rate_1h": weighted_refund_rate(roi_items) if roi_items else None,
    }


def _merge_refund_rate(parts: list[dict[str, Any]]) -> float | None:
    """多天或多个人合并时，1 小时退货率按支付金额加权，不能把百分数直接相加。"""
    total_pay = 0.0
    weighted = 0.0
    for part in parts:
        pay = float(part.get("pay_amount") or 0)
        rate = part.get("refund_rate_1h")
        if pay <= 0 or rate is None:
            continue
        total_pay += pay
        weighted += pay * float(rate)
    return round(weighted / total_pay, 6) if total_pay > 0 else None


def _weighted_from_parts(parts: list[dict[str, Any]]) -> tuple[float | None, float | None]:
    """多天或分段合并时，用各段 pay_roi / settle_roi 按消耗加权。"""
    total_cost = 0.0
    pay_weight = 0.0
    settle_weight = 0.0
    for part in parts:
        cost = float(part.get("stat_cost") or 0)
        if cost <= 0:
            continue
        total_cost += cost
        pay_roi = part.get("pay_roi")
        if pay_roi is not None:
            pay_weight += cost * float(pay_roi)
        settle_roi = part.get("settle_roi")
        if settle_roi is not None:
            settle_weight += cost * float(settle_roi)
    if total_cost <= 0:
        return None, None
    pay_roi = round(pay_weight / total_cost, 4) if pay_weight else None
    settle_roi = round(settle_weight / total_cost, 4) if settle_weight else None
    return pay_roi, settle_roi


def _chosen(accounts: list[dict[str, Any]], person: str | None) -> list[tuple[str, dict[str, Any]]]:
    """和子账户页一样，只保留英文括号里的人名。person 为空时保留全部。"""
    chosen: list[tuple[str, dict[str, Any]]] = []
    for item in accounts:
        name = str(item.get("advertiser_name") or "")
        if not _has_parentheses(name):
            continue
        who = person_name(name)
        if not who:
            continue
        if person and who != person:
            continue
        chosen.append((who, item))
    return chosen


def rollup_accounts(accounts: list[dict[str, Any]], person: str | None = None) -> dict[str, Any]:
    chosen = _chosen(accounts, person)
    bucket = _blank()
    items: list[dict[str, Any]] = []
    for _who, item in chosen:
        _add(bucket, item)
        items.append(item)
    return _finish(bucket, items)


def rollup_people(accounts: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """全部用空字符串，另外每个运营一行。"""
    buckets: dict[str, dict[str, float | int]] = defaultdict(_blank)
    items_by_person: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for who, item in _chosen(accounts, None):
        _add(buckets[ALL_PERSON], item)
        items_by_person[ALL_PERSON].append(item)
        _add(buckets[who], item)
        items_by_person[who].append(item)
    return {key: _finish(bucket, items_by_person[key]) for key, bucket in buckets.items()}


def save_overview_day(db: Session, day: str, accounts: list[dict[str, Any]]) -> int:
    """把某一天的工作台汇总写入日表。已有行覆盖。返回写入的人数（含全部）。"""
    stat_date = date.fromisoformat(day)
    rows = rollup_people(accounts)
    if not rows:
        rows = {ALL_PERSON: _finish(_blank(), [])}
    official = take_roi2_total(day, day) or {}
    official_rate = official.get("refund_rate_1h")
    for person, metrics in rows.items():
        rate = metrics.get("refund_rate_1h")
        if person == ALL_PERSON and official_rate is not None:
            rate = official_rate
        stmt = mysql_insert(QianchuanOverviewDaily).values(
            stat_date=stat_date,
            person=person,
            stat_cost=metrics["stat_cost"],
            pay_amount=metrics["pay_amount"],
            settle_amount=metrics["settle_amount"],
            order_count=0,
            pay_roi=metrics["pay_roi"],
            settle_roi=metrics["settle_roi"],
            order_cost=None,
            refund_rate_1h=rate,
        )
        updates = {
            "stat_cost": stmt.inserted.stat_cost,
            "pay_amount": stmt.inserted.pay_amount,
            "settle_amount": stmt.inserted.settle_amount,
            "pay_roi": stmt.inserted.pay_roi,
            "settle_roi": stmt.inserted.settle_roi,
        }
        # 全部这一行的退货率只接受工作台合计。这次没拿到合计时，不要用各账户加权把原站的数盖掉。
        if not (person == ALL_PERSON and official_rate is None):
            updates["refund_rate_1h"] = stmt.inserted.refund_rate_1h
        stmt = stmt.on_duplicate_key_update(**updates)
        db.execute(stmt)
    db.commit()
    return len(rows)


def _sum_rows(rows: list[QianchuanOverviewDaily]) -> dict[str, Any]:
    bucket = _blank()
    parts: list[dict[str, Any]] = []
    for row in rows:
        _add(
            bucket,
            {
                "stat_cost": row.stat_cost,
                "pay_amount": row.pay_amount,
                "settle_amount": row.settle_amount,
            },
        )
        parts.append(
            {
                "stat_cost": float(row.stat_cost or 0),
                "pay_amount": float(row.pay_amount or 0),
                "pay_roi": float(row.pay_roi) if row.pay_roi is not None else None,
                "settle_roi": float(row.settle_roi) if row.settle_roi is not None else None,
                "refund_rate_1h": float(row.refund_rate_1h) if row.refund_rate_1h is not None else None,
            }
        )
    out = _finish(bucket, [])
    pay_roi, settle_roi = _weighted_from_parts(parts)
    out["pay_roi"] = pay_roi
    out["settle_roi"] = settle_roi
    out["refund_rate_1h"] = _merge_refund_rate(parts)
    return out


def _advertiser_ids(db: Session, person: str | None) -> list[int]:
    ids: list[int] = []
    for row in db.scalars(select(QianchuanAdvertiser)).all():
        name = row.advertiser_name or ""
        if not _has_parentheses(name):
            continue
        who = person_name(name)
        if not who:
            continue
        if person and who != person:
            continue
        ids.append(int(row.advertiser_id))
    return ids


def _from_account_daily(db: Session, days: list[str], person: str | None) -> dict[str, Any]:
    """日汇总还没入库的日期，用账户日表补消耗、支付和净成交。"""
    ids = _advertiser_ids(db, person)
    bucket = _blank()
    if not ids or not days:
        return _finish(bucket, [])
    stat_days = [date.fromisoformat(day) for day in days]
    rows = db.execute(
        select(
            QianchuanAccountDaily.cost,
            QianchuanAccountDaily.pay_amount,
            QianchuanAccountDaily.settle_amount,
            QianchuanAccountDaily.pay_roi,
            QianchuanAccountDaily.settle_roi,
            QianchuanAccountDaily.refund_rate_1h,
        ).where(
            QianchuanAccountDaily.advertiser_id.in_(ids),
            QianchuanAccountDaily.stat_date.in_(stat_days),
        )
    ).all()
    items: list[dict[str, Any]] = []
    for cost, pay, settle, pay_roi, settle_roi, refund_rate in rows:
        _add(bucket, {"stat_cost": cost, "pay_amount": pay, "settle_amount": settle})
        items.append(
            {
                "stat_cost": float(cost or 0),
                "pay_amount": float(pay or 0),
                "pay_roi": float(pay_roi) if pay_roi is not None else None,
                "settle_roi": float(settle_roi) if settle_roi is not None else None,
                "refund_rate_1h": float(refund_rate) if refund_rate is not None else None,
            }
        )
    out = _finish(bucket, [])
    pay_roi, settle_roi = _weighted_from_parts(items)
    out["pay_roi"] = pay_roi
    out["settle_roi"] = settle_roi
    out["refund_rate_1h"] = weighted_refund_rate(items)
    return out


def read_stored_range(db: Session, start: str, end: str, person: str | None) -> tuple[dict[str, Any], list[str]]:
    """读已入库的日汇总。返回合计，以及还没有汇总行的日期。"""
    key = person or ALL_PERSON
    start_d = date.fromisoformat(start)
    end_d = date.fromisoformat(end)
    rows = db.scalars(
        select(QianchuanOverviewDaily).where(
            QianchuanOverviewDaily.person == key,
            QianchuanOverviewDaily.stat_date >= start_d,
            QianchuanOverviewDaily.stat_date <= end_d,
        )
    ).all()
    have = {row.stat_date.isoformat() for row in rows}
    missing: list[str] = []
    cursor = start_d
    while cursor <= end_d:
        day = cursor.isoformat()
        if day not in have:
            missing.append(day)
        cursor += timedelta(days=1)
    stored = _sum_rows(rows)
    if missing:
        stored = _finish_sum(stored, _from_account_daily(db, missing, person))
    return stored, missing


def _finish_sum(left: dict[str, Any], right: dict[str, Any]) -> dict[str, Any]:
    bucket = _blank()
    _add(bucket, left)
    _add(bucket, right)
    out = _finish(bucket, [])
    pay_roi, settle_roi = _weighted_from_parts([left, right])
    out["pay_roi"] = pay_roi
    out["settle_roi"] = settle_roi
    out["refund_rate_1h"] = _merge_refund_rate([left, right])
    return out


def empty_metrics() -> dict[str, Any]:
    return _finish(_blank(), [])


async def metrics_for_range(
    db: Session,
    start: str,
    end: str,
    person: str | None,
    today_part: dict[str, Any] | None = None,
    *,
    fetch_today: bool = True,
) -> dict[str, Any]:
    """区间汇总。昨天及以前走日表。今天用已发布的工作台汇总，不再为了页面去打工作台。"""
    today = date.today().isoformat()
    if end > today:
        end = today
    yesterday = (date.today() - timedelta(days=1)).isoformat()
    include_today = start <= today <= end
    history_end = yesterday if end >= today else end
    total = _finish(_blank(), [])
    if start <= history_end:
        stored, _missing = read_stored_range(db, start, history_end, person)
        total = _finish_sum(total, stored)
    if include_today:
        if today_part is not None:
            total = _finish_sum(total, today_part)
        elif fetch_today:
            accounts = await load_roi2_accounts(today, today)
            total = _finish_sum(total, rollup_accounts(accounts, person))
    stored_rate = total.get("refund_rate_1h")
    official = await official_refund_rate(db, start, end, person)
    total["refund_rate_1h"] = official if official is not None else stored_rate
    return total


def _store_refund_rate(db: Session, day: str, person: str, rate: float) -> None:
    """把原站合计退货率写回这一天。没有这一行就不新建。"""
    stat_date = date.fromisoformat(day)
    row = db.scalar(
        select(QianchuanOverviewDaily).where(
            QianchuanOverviewDaily.stat_date == stat_date,
            QianchuanOverviewDaily.person == person,
        )
    )
    if row is None:
        return
    row.refund_rate_1h = rate
    db.commit()


async def official_refund_rate(db: Session, start: str, end: str, person: str | None) -> float | None:
    """这一段的 1 小时退货率直接用工作台合计。15 分钟内同一段不重复拉。失败时返回 None。"""
    person_key = person or ALL_PERSON
    cache_key = f"ad_helper:roi2_total:v1:{start}:{end}:{person_key}"
    try:
        raw = await get_cache_redis().get(cache_key)
        if raw:
            data = json.loads(raw)
            if data.get("refund_rate_1h") is not None:
                return float(data["refund_rate_1h"])
    except Exception as exc:  # noqa: BLE001
        logger.warning("读原站退货率缓存失败：%s", exc)
    ids = _advertiser_ids(db, person) if person else None
    if person and not ids:
        return None
    try:
        total = await fetch_roi2_total(start, end, ids)
    except Exception as exc:  # noqa: BLE001
        logger.warning("按原站重拉 %s~%s 退货率失败：%s", start, end, exc)
        return None
    rate = total.get("refund_rate_1h")
    try:
        await get_cache_redis().set(
            cache_key,
            json.dumps({"refund_rate_1h": rate}),
            ex=_REFUND_CACHE_SECONDS,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("写原站退货率缓存失败：%s", exc)
    if start == end and rate is not None:
        _store_refund_rate(db, start, person_key, float(rate))
    return None if rate is None else float(rate)


def with_profit_rate(metrics: dict[str, Any], profit: float) -> dict[str, Any]:
    pay = float(metrics.get("pay_amount") or 0)
    out = dict(metrics)
    out["profit_rate"] = round(float(profit) / pay, 6) if pay > 0 else None
    return out
