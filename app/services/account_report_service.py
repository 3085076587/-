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


def has_account_day(db: Session, day: str | date) -> bool:
    """某天是否已有账户日数据（任意账户）。"""
    stat_date = _as_date(day)
    count = db.scalar(
        select(func.count())
        .select_from(QianchuanAccountDaily)
        .where(QianchuanAccountDaily.stat_date == stat_date)
    )
    return int(count or 0) > 0


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
