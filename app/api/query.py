"""前端页面用的本地查询接口（读库，不打巨量）。"""

from datetime import date, datetime

from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.deps import get_db
from app.core.response import ok
from app.models.ad_plan import QianchuanAdPlan
from app.models.ad_report import QianchuanAdReport
from app.models.advertiser import QianchuanAdvertiser
from app.services import token_store
from app.services.account_access import assert_advertiser_access, current_username, is_admin
from app.services.advertiser_dashboard import _has_parentheses, person_name

router = APIRouter(tags=["Query"])


def _dt(value: datetime | date | None) -> str | None:
    if value is None:
        return None
    return value.isoformat(sep=" ", timespec="seconds") if isinstance(value, datetime) else value.isoformat()


@router.get("/oauth/status", summary="查询本地 token 状态")
def oauth_status(db: Session = Depends(get_db)):
    row = token_store.get_token_row(db)
    if row is None:
        return ok(
            {
                "has_token": False,
                "agent_id": None,
                "expires_at": None,
                "refresh_expires_at": None,
                "access_expired": True,
            }
        )
    now = datetime.now()
    return ok(
        {
            "has_token": True,
            "agent_id": row.agent_id,
            "agent_name": getattr(row, "agent_name", None),
            "account_role": getattr(row, "account_role", None),
            "expires_at": _dt(row.expires_at),
            "refresh_expires_at": _dt(row.refresh_expires_at),
            "access_expired": row.expires_at <= now,
            "refresh_expired": row.refresh_expires_at <= now,
        }
    )


@router.get("/advertiser/list", summary="查询已同步的千川子账户")
def list_advertisers(request: Request, db: Session = Depends(get_db)):
    username = current_username(request)
    rows = db.scalars(
        select(QianchuanAdvertiser).order_by(QianchuanAdvertiser.advertiser_id.asc())
    ).all()
    visible = []
    for r in rows:
        adv_name = r.advertiser_name or ""
        if not _has_parentheses(adv_name):
            continue
        if not is_admin(username) and person_name(adv_name) != username:
            continue
        visible.append(
            {
                "id": r.id,
                "agent_id": r.agent_id,
                "advertiser_id": r.advertiser_id,
                "advertiser_name": r.advertiser_name,
                "status": r.status,
                "sync_time": _dt(r.sync_time),
            }
        )
    return ok(visible)


@router.get("/plan/list", summary="查询已同步的计划")
def list_plans(
    request: Request,
    advertiser_id: int = Query(..., description="千川子账户 ID"),
    db: Session = Depends(get_db),
):
    assert_advertiser_access(db, current_username(request), advertiser_id)
    rows = db.scalars(
        select(QianchuanAdPlan)
        .where(QianchuanAdPlan.advertiser_id == advertiser_id)
        .order_by(QianchuanAdPlan.ad_id.desc())
    ).all()
    return ok(
        [
            {
                "id": r.id,
                "advertiser_id": r.advertiser_id,
                "ad_id": r.ad_id,
                "ad_name": r.ad_name,
                "roi2_goal": float(r.roi2_goal) if r.roi2_goal is not None else None,
                "plan_status": r.plan_status,
                "sync_time": _dt(r.sync_time),
            }
            for r in rows
        ]
    )


@router.get("/plan/report/list", summary="查询已同步的计划报表")
def list_reports(
    request: Request,
    advertiser_id: int = Query(..., description="千川子账户 ID"),
    start_date: str | None = Query(None, description="开始日期 yyyy-MM-dd"),
    end_date: str | None = Query(None, description="结束日期 yyyy-MM-dd"),
    db: Session = Depends(get_db),
):
    assert_advertiser_access(db, current_username(request), advertiser_id)
    stmt = select(QianchuanAdReport).where(QianchuanAdReport.advertiser_id == advertiser_id)
    if start_date:
        stmt = stmt.where(QianchuanAdReport.stat_date >= start_date)
    if end_date:
        stmt = stmt.where(QianchuanAdReport.stat_date <= end_date)
    rows = db.scalars(stmt.order_by(QianchuanAdReport.stat_date.desc(), QianchuanAdReport.ad_id.desc())).all()
    return ok(
        [
            {
                "id": r.id,
                "advertiser_id": r.advertiser_id,
                "ad_id": r.ad_id,
                "stat_date": _dt(r.stat_date),
                "cost": float(r.cost or 0),
                "pay_order_count": r.pay_order_count,
                "pay_amount": float(r.pay_amount or 0),
                "roi": float(r.roi) if r.roi is not None else None,
                "sync_time": _dt(r.sync_time),
            }
            for r in rows
        ]
    )
