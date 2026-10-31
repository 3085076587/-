"""全域计划：实时列表、启停、改 ROI。"""

from __future__ import annotations

import asyncio
from datetime import date
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.clients.oceanengine import OceanEngineClient
from app.core.exceptions import AppError, OceanEngineApiError
from app.models.plan_commission import PlanCommission
from app.schemas.plan import UpdateRoiIn, UpdateRoiOut, UpdateStatusIn
from app.services import reminder_service, token_store

PLAN_FIELDS = [
    "stat_cost",
    "total_pay_order_gmv_for_roi2",
    "total_prepay_and_pay_order_roi2",
    "total_order_settle_amount_for_roi2_1h",
    "total_prepay_and_pay_settle_roi2_1h",
    "total_refund_order_gmv_for_roi2_1h_rate",
    "total_refund_order_gmv_for_roi2_1h_all",
]


def _num(value: Any, default: float = 0.0) -> float:
    try:
        return float(value) if value not in (None, "") else default
    except (TypeError, ValueError):
        return default


def _mf_to_yuan(value: Any) -> float:
    return round(_num(value) / 100000.0, 2)


def _norm_rate(value: Any) -> float | None:
    rate = _num(value, default=-1)
    if rate < 0:
        return None
    while rate > 1 and rate <= 10000:
        rate = rate / 100.0
    if rate > 1:
        return None
    return round(rate, 6)


_STATUS_LABELS = {
    "DELIVERY_OK": "投放中",
    "ENABLE": "投放中",
    "DELIVERING": "投放中",
    "DISABLE": "已暂停",
    "DISABLED": "已暂停",
    "PAUSED": "已暂停",
    "SYSTEM_DISABLE": "系统暂停",
    "OFFLINE_BUDGET": "预算不足",
    "OFFLINE_BALANCE": "余额不足",
    "AUDIT": "审核中",
    "REAUDIT": "审核中",
    "AUDIT_DENY": "审核不通过",
    "TIME_NO_REACH": "未到投放时间",
    "NO_SCHEDULE": "不在投放时段",
    "FROZEN": "已终止",
    "DELETE": "已删除",
    "DELETED": "已删除",
}


def _status_label(ad_info: dict[str, Any]) -> str:
    status = str(ad_info.get("status") or "")
    opt = str(ad_info.get("opt_status") or "")
    if status in _STATUS_LABELS:
        return _STATUS_LABELS[status]
    if opt == "DISABLE":
        return "已暂停"
    if opt == "ENABLE":
        return "投放中"
    return status or opt or "-"


async def list_uni_plans(
    db: Session,
    advertiser_id: int,
    *,
    start_date: str | None = None,
    end_date: str | None = None,
) -> dict[str, Any]:
    today = date.today().isoformat()
    start = start_date or today
    end = end_date or today
    start_time = f"{start} 00:00:00"
    end_time = f"{end} 23:59:59"
    client = await token_store.build_client(db)
    saved_commission = {
        int(row.ad_id): float(row.commission_pct)
        for row in db.scalars(
            select(PlanCommission).where(PlanCommission.advertiser_id == advertiser_id)
        ).all()
        if float(row.commission_pct) > 0
    }

    plans: list[dict[str, Any]] = []
    page = 1
    while True:
        data = None
        last_exc: OceanEngineApiError | None = None
        for attempt in range(3):
            try:
                data = await client.list_uni_promotion(
                    advertiser_id,
                    start_time,
                    end_time,
                    marketing_goal="VIDEO_PROM_GOODS",
                    fields=PLAN_FIELDS,
                    page=page,
                    page_size=50,
                )
                break
            except OceanEngineApiError as exc:
                last_exc = exc
                if "超时" not in str(exc) or attempt == 2:
                    raise
                await asyncio.sleep(1.2 * (attempt + 1))
        if data is None:
            raise last_exc or AppError("拉取计划失败")
        for item in data.get("ad_list") or []:
            ad_info = item.get("ad_info") or {}
            stats = item.get("stats_info") or {}
            products = item.get("product_info") or []
            product_name = ""
            product_image = ""
            if products and isinstance(products[0], dict):
                product_name = str(products[0].get("product_name") or "")
                product_image = str(products[0].get("product_image") or "")
            ad_id = int(ad_info.get("id") or 0)
            if not ad_id:
                continue
            cost = _mf_to_yuan(stats.get("stat_cost"))
            settle = _mf_to_yuan(stats.get("total_order_settle_amount_for_roi2_1h"))
            pay = _mf_to_yuan(stats.get("total_pay_order_gmv_for_roi2"))
            pay_roi = _num(stats.get("total_prepay_and_pay_order_roi2")) or None
            pay_raw = _num(stats.get("total_pay_order_gmv_for_roi2"))
            refund_gmv = _num(stats.get("total_refund_order_gmv_for_roi2_1h_all"))
            api_rate = _norm_rate(stats.get("total_refund_order_gmv_for_roi2_1h_rate"))
            if pay_raw > 0 and refund_gmv > 0:
                refund_rate = round(min(1.0, refund_gmv / pay_raw), 6)
            else:
                refund_rate = api_rate
            plans.append(
                {
                    "advertiser_id": advertiser_id,
                    "ad_id": ad_id,
                    "ad_name": str(ad_info.get("name") or ""),
                    "product_name": product_name,
                    "product_image": product_image,
                    "opt_status": str(ad_info.get("opt_status") or ""),
                    "status": str(ad_info.get("status") or ""),
                    "status_label": _status_label(ad_info),
                    "enabled": str(ad_info.get("opt_status") or "") != "DISABLE",
                    "budget": ad_info.get("budget"),
                    "budget_mode": ad_info.get("budget_mode"),
                    "roi2_goal": ad_info.get("roi2_goal"),
                    "deep_external_action": ad_info.get("deep_external_action"),
                    "stat_cost": cost,
                    "pay_amount": pay,
                    "pay_roi": pay_roi,
                    "settle_amount": settle,
                    "settle_roi": _num(stats.get("total_prepay_and_pay_settle_roi2_1h")) or None,
                    "refund_rate_1h": refund_rate,
                    "commission_pct": saved_commission.get(ad_id),
                }
            )
        page_info = data.get("page_info") or {}
        batch = data.get("ad_list") or []
        total_number = int(page_info.get("total_number") or page_info.get("total_num") or 0)
        total_page = int(page_info.get("total_page") or 0)
        if total_number > 0:
            has_more = page * 50 < total_number
        elif total_page > 0:
            has_more = page < total_page
        else:
            has_more = len(batch) >= 50
        if not batch or not has_more:
            break
        page += 1

    return {
        "advertiser_id": advertiser_id,
        "start_date": start,
        "end_date": end,
        "plans": plans,
        "total": len(plans),
    }


def save_plan_commission(
    db: Session,
    advertiser_id: int,
    ad_id: int,
    commission_pct: float | None,
) -> dict[str, Any]:
    """佣金为空或 0 时删掉记录，页面上不显示利润。"""
    row = db.scalar(
        select(PlanCommission).where(
            PlanCommission.advertiser_id == advertiser_id,
            PlanCommission.ad_id == ad_id,
        )
    )
    pct = float(commission_pct or 0)
    if pct <= 0:
        if row is not None:
            db.delete(row)
            db.commit()
        return {"advertiser_id": advertiser_id, "ad_id": ad_id, "commission_pct": None}
    if row is None:
        row = PlanCommission(advertiser_id=advertiser_id, ad_id=ad_id, commission_pct=pct)
        db.add(row)
    else:
        row.commission_pct = pct
    db.commit()
    return {"advertiser_id": advertiser_id, "ad_id": ad_id, "commission_pct": pct}


async def update_plan_status(db: Session, payload: UpdateStatusIn) -> dict[str, Any]:
    if payload.opt_status not in {"ENABLE", "DISABLE"}:
        raise AppError("opt_status 只允许 ENABLE / DISABLE")
    client = await token_store.build_client(db)
    data = await client.update_uni_promotion_status(
        payload.advertiser_id,
        [payload.ad_id],
        payload.opt_status,
    )
    return {
        "advertiser_id": payload.advertiser_id,
        "ad_id": payload.ad_id,
        "opt_status": payload.opt_status,
        "oceanengine": data if isinstance(data, dict) else {"raw": data},
    }


async def update_roi_with_reminder(db: Session, payload: UpdateRoiIn) -> dict[str, Any]:
    client = await token_store.build_client(db)
    ocean_data = await client.update_roi2_goal(
        payload.advertiser_id,
        payload.ad_id,
        payload.roi2_goal,
        deep_external_action=payload.deep_external_action
        or "AD_CONVERT_TYPE_LIVE_PURE_PAY_ROI",
    )
    reminder = None
    if payload.remind_after_seconds and payload.remind_after_seconds > 0:
        reminder = await reminder_service.create_reminder(
            advertiser_id=payload.advertiser_id,
            ad_id=payload.ad_id,
            ad_name=payload.ad_name or "",
            roi2_goal=payload.roi2_goal,
            remind_after_seconds=payload.remind_after_seconds,
        )
    out = UpdateRoiOut(
        advertiser_id=payload.advertiser_id,
        ad_id=payload.ad_id,
        roi2_goal=payload.roi2_goal,
        oceanengine=ocean_data if isinstance(ocean_data, dict) else {"raw": ocean_data},
    ).model_dump()
    out["reminder"] = reminder
    return out
