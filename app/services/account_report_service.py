"""账户按日数据：复用子账户页的工作台账户表（load_roi2_accounts）逐日入库。

只被回填脚本和总览自愈调用；工作台抓取本身不改。
"""

from __future__ import annotations

import logging
from datetime import date, datetime
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.dialects.mysql import insert as mysql_insert
from sqlalchemy.orm import Session

from app.models.qianchuan_account_daily import QianchuanAccountDaily
from app.services import advertiser_dashboard

logger = logging.getLogger("uvicorn.error")


async def sync_account_day(db: Session, day: str) -> int:
    """拉某天全部账户的工作台指标写入账户日表，返回行数。

    复用 load_roi2_accounts（一次请求返回全部账户），不改子账户页逻辑。
    """
    accounts = await advertiser_dashboard.load_roi2_accounts(day, day)
    return upsert_account_day_rows(db, day, accounts)


def upsert_account_day_rows(db: Session, day: str, accounts: list[dict[str, Any]]) -> int:
    """把已拉取的工作台账户行写入账户日表（供每日同步复用同一次请求）。"""
    stat_date = datetime.strptime(day, "%Y-%m-%d").date()
    rows = 0
    for item in accounts:
        adv_id = int(item.get("advertiser_id") or 0)
        if not adv_id:
            continue
        stmt = mysql_insert(QianchuanAccountDaily).values(
            advertiser_id=adv_id,
            stat_date=stat_date,
            cost=float(item.get("stat_cost") or 0),
            pay_amount=float(item.get("pay_amount") or 0),
            pay_roi=item.get("pay_roi"),
            settle_amount=item.get("settle_amount"),
            settle_roi=item.get("settle_roi"),
            refund_rate_1h=item.get("refund_rate_1h"),
        )
        stmt = stmt.on_duplicate_key_update(
            cost=stmt.inserted.cost,
            pay_amount=stmt.inserted.pay_amount,
            pay_roi=stmt.inserted.pay_roi,
            settle_amount=stmt.inserted.settle_amount,
            settle_roi=stmt.inserted.settle_roi,
            refund_rate_1h=stmt.inserted.refund_rate_1h,
        )
        db.execute(stmt)
        rows += 1
    db.commit()
    return rows


def fill_missing_account_days_zero(db: Session, day: str, advertiser_ids: list[int]) -> int:
    """工作台未返回的户补零行，避免「库里有户、日表无行」导致验收永远不过。"""
    missing = advertisers_missing_account_day(db, day, advertiser_ids)
    if not missing:
        return 0
    stat_date = datetime.strptime(day, "%Y-%m-%d").date()
    for adv_id in missing:
        stmt = mysql_insert(QianchuanAccountDaily).values(
            advertiser_id=int(adv_id),
            stat_date=stat_date,
            cost=0.0,
            pay_amount=0.0,
            pay_roi=None,
            settle_amount=None,
            settle_roi=None,
            refund_rate_1h=None,
        )
        stmt = stmt.on_duplicate_key_update(cost=stmt.inserted.cost)
        db.execute(stmt)
    db.commit()
    return len(missing)


def advertisers_with_account_day(db: Session, day: str | date, advertiser_ids: list[int]) -> set[int]:
    """这些账户里，哪些在指定日已有账户日表行。"""
    if not advertiser_ids:
        return set()
    stat_date = _as_date(day)
    rows = db.execute(
        select(QianchuanAccountDaily.advertiser_id).where(
            QianchuanAccountDaily.stat_date == stat_date,
            QianchuanAccountDaily.advertiser_id.in_(advertiser_ids),
        )
    ).all()
    return {int(r[0]) for r in rows}


def advertisers_missing_account_day(db: Session, day: str | date, advertiser_ids: list[int]) -> list[int]:
    have = advertisers_with_account_day(db, day, advertiser_ids)
    return [int(a) for a in advertiser_ids if int(a) not in have]


def has_account_day(db: Session, day: str | date) -> bool:
    """某天是否已有账户日数据（任意账户）。"""
    stat_date = _as_date(day)
    count = db.scalar(
        select(func.count())
        .select_from(QianchuanAccountDaily)
        .where(QianchuanAccountDaily.stat_date == stat_date)
    )
    return int(count or 0) > 0


def account_cost_by_day(
    db: Session,
    advertiser_ids: list[int],
    start: str | date,
    end: str | date,
) -> dict[str, float]:
    """这些账户在区间内每一天的消耗合计。没有行的日期不出现在结果里。"""
    if not advertiser_ids:
        return {}
    rows = db.execute(
        select(
            QianchuanAccountDaily.stat_date,
            func.sum(QianchuanAccountDaily.cost),
        )
        .where(
            QianchuanAccountDaily.advertiser_id.in_(advertiser_ids),
            QianchuanAccountDaily.stat_date >= _as_date(start),
            QianchuanAccountDaily.stat_date <= _as_date(end),
        )
        .group_by(QianchuanAccountDaily.stat_date)
    ).all()
    return {
        _day_key(stat_date): round(float(cost or 0), 2)
        for stat_date, cost in rows
    }


def account_cost_by_advertiser(
    db: Session, start: str | date, end: str | date
) -> dict[int, float]:
    """区间内每个账户的消耗合计（元），供总览按人聚合。"""
    rows = db.execute(
        select(
            QianchuanAccountDaily.advertiser_id,
            func.sum(QianchuanAccountDaily.cost),
        )
        .where(
            QianchuanAccountDaily.stat_date >= _as_date(start),
            QianchuanAccountDaily.stat_date <= _as_date(end),
        )
        .group_by(QianchuanAccountDaily.advertiser_id)
    ).all()
    return {int(adv_id): float(cost or 0) for adv_id, cost in rows}


def account_rows_by_advertiser(
    db: Session, start: str | date, end: str | date
) -> dict[int, dict[str, Any]]:
    """区间内每个账户的 6 指标合计/均值，供后续扩展展示。"""
    rows = db.execute(
        select(
            QianchuanAccountDaily.advertiser_id,
            func.sum(QianchuanAccountDaily.cost),
            func.sum(QianchuanAccountDaily.pay_amount),
            func.sum(QianchuanAccountDaily.settle_amount),
        )
        .where(
            QianchuanAccountDaily.stat_date >= _as_date(start),
            QianchuanAccountDaily.stat_date <= _as_date(end),
        )
        .group_by(QianchuanAccountDaily.advertiser_id)
    ).all()
    out: dict[int, dict[str, Any]] = {}
    for adv_id, cost, pay, settle in rows:
        cost = float(cost or 0)
        pay = float(pay or 0)
        settle_val = float(settle or 0)
        out[int(adv_id)] = {
            "cost": round(cost, 2),
            "pay_amount": round(pay, 2),
            "pay_roi": round(pay / cost, 4) if cost > 0 else None,
            "settle_amount": round(settle_val, 2),
            "settle_roi": round(settle_val / cost, 4) if cost > 0 else None,
        }
    return out


def _as_date(value: str | date) -> date:
    if isinstance(value, date) and not isinstance(value, datetime):
        return value
    if isinstance(value, datetime):
        return value.date()
    return datetime.strptime(str(value), "%Y-%m-%d").date()


def _day_key(value: date | datetime | str) -> str:
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    return str(value)[:10]
