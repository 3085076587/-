"""全域计划：实时列表、启停、改 ROI。"""

from __future__ import annotations

import asyncio
from datetime import date
from typing import Any

from sqlalchemy.orm import Session

from app.clients.oceanengine import OceanEngineClient
from app.core.exceptions import AppError, OceanEngineApiError
from app.schemas.plan import UpdateRoiIn, UpdateRoiOut, UpdateStatusIn
from app.services import reminder_service, token_store

PLAN_FIELDS = [
    "stat_cost",
    "total_pay_order_gmv_for_roi2",
    "total_prepay_and_pay_order_roi2",
    "total_order_settle_amount_for_roi2_1h",
    "total_prepay_and_pay_settle_roi2_1h",
    "total_refund_order_gmv_for_roi2_1h_rate",
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


def _status_label(ad_info: dict[str, Any]) -> str:
    status = str(ad_info.get("status") or "")
    opt = str(ad_info.get("opt_status") or "")
    if opt == "DISABLE" or status in {"DISABLE", "DISABLED", "PAUSED"}:
        return "已暂停"
    if status in {"DELIVERY_OK", "ENABLE", "DELIVERING"} or opt == "ENABLE":
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
            refund_rate = _norm_rate(stats.get("total_refund_order_gmv_for_roi2_1h_rate"))
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
                }
            )
        page_info = data.get("page_info") or {}
        total_page = int(page_info.get("total_page") or page)
        if page >= total_page or not (data.get("ad_list") or []):
            break
        page += 1

    return {
        "advertiser_id": advertiser_id,
        "start_date": start,
        "end_date": end,
        "plans": plans,
        "total": len(plans),
    }


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
