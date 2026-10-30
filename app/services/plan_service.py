"""计划同步、报表同步、ROI 修改。"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.ad_plan import QianchuanAdPlan
from app.models.ad_report import QianchuanAdReport
from app.schemas.plan import PlanReportSyncOut, PlanSyncOut, UpdateRoiIn, UpdateRoiOut
from app.services import token_store


def _to_decimal(value: object) -> Decimal | None:
    if value is None or value == "":
        return None
    return Decimal(str(value))


def _extract_roi2_goal(item: dict) -> Decimal | None:
    for key in ("roi2_goal", "roi_goal", "deep_external_action"):
        if key in item and item.get(key) not in (None, ""):
            try:
                return _to_decimal(item.get(key))
            except Exception:
                continue
    # 嵌套字段兜底。
    for nest_key in ("delivery_setting", "audience"):
        nest = item.get(nest_key) or {}
        if isinstance(nest, dict):
            for key in ("roi2_goal", "roi_goal"):
                if nest.get(key) not in (None, ""):
                    return _to_decimal(nest.get(key))
    return None


async def sync_plans(db: Session, advertiser_id: int) -> PlanSyncOut:
    client = await token_store.build_client(db)
    page = 1
    page_size = 100
    created = 0
    updated = 0
    total = 0
    now = datetime.now().replace(microsecond=0)

    while True:
        data = await client.list_ads(advertiser_id, page=page, page_size=page_size)
        items = data.get("list") or data.get("ad_list") or []
        total = int((data.get("page_info") or {}).get("total_number") or total or len(items))

        for item in items:
            ad_id = int(item.get("ad_id") or item.get("id") or 0)
            if not ad_id:
                continue
            ad_name = str(item.get("name") or item.get("ad_name") or "")
            plan_status = str(item.get("status") or item.get("opt_status") or "")
            roi2_goal = _extract_roi2_goal(item)

            existing = db.scalar(
                select(QianchuanAdPlan).where(
                    QianchuanAdPlan.advertiser_id == advertiser_id,
                    QianchuanAdPlan.ad_id == ad_id,
                )
            )
            if existing is None:
                db.add(
                    QianchuanAdPlan(
                        advertiser_id=advertiser_id,
                        ad_id=ad_id,
                        ad_name=ad_name,
                        roi2_goal=roi2_goal,
                        plan_status=plan_status,
                        sync_time=now,
                    )
                )
                created += 1
            else:
                existing.ad_name = ad_name or existing.ad_name
                existing.plan_status = plan_status or existing.plan_status
                if roi2_goal is not None:
                    existing.roi2_goal = roi2_goal
                existing.sync_time = now
                updated += 1

        page_info = data.get("page_info") or {}
        total_page = int(page_info.get("total_page") or page)
        if page >= total_page or not items:
            break
        page += 1

    db.commit()
    return PlanSyncOut(
        advertiser_id=advertiser_id,
        total=total,
        created=created,
        updated=updated,
    )


async def sync_plan_reports(
    db: Session,
    advertiser_id: int,
    start_date: str,
    end_date: str,
) -> PlanReportSyncOut:
    client = await token_store.build_client(db)
    page = 1
    page_size = 100
    created = 0
    updated = 0
    total = 0
    now = datetime.now().replace(microsecond=0)

    while True:
        data = await client.get_ad_report(
            advertiser_id,
            start_date,
            end_date,
            page=page,
            page_size=page_size,
        )
        items = data.get("list") or data.get("rows") or []
        total = int((data.get("page_info") or {}).get("total_number") or total or len(items))

        for item in items:
            ad_id = int(item.get("ad_id") or item.get("id") or 0)
            if not ad_id:
                continue

            # 天粒度字段可能是 stat_datetime / stat_time_day。
            raw_date = (
                item.get("stat_datetime")
                or item.get("stat_time_day")
                or item.get("date")
                or start_date
            )
            stat_date = str(raw_date)[:10]

            cost = _to_decimal(item.get("stat_cost") or item.get("cost") or 0) or Decimal("0")
            pay_order_count = int(item.get("pay_order_count") or 0)
            pay_amount = _to_decimal(
                item.get("pay_order_amount") or item.get("pay_amount") or 0
            ) or Decimal("0")
            roi = _to_decimal(
                item.get("prepay_and_pay_order_roi")
                or item.get("create_order_roi")
                or item.get("roi")
            )

            existing = db.scalar(
                select(QianchuanAdReport).where(
                    QianchuanAdReport.advertiser_id == advertiser_id,
                    QianchuanAdReport.ad_id == ad_id,
                    QianchuanAdReport.stat_date == stat_date,
                )
            )
            if existing is None:
                db.add(
                    QianchuanAdReport(
                        advertiser_id=advertiser_id,
                        ad_id=ad_id,
                        stat_date=stat_date,
                        cost=cost,
                        pay_order_count=pay_order_count,
                        pay_amount=pay_amount,
                        roi=roi,
                        sync_time=now,
                    )
                )
                created += 1
            else:
                existing.cost = cost
                existing.pay_order_count = pay_order_count
                existing.pay_amount = pay_amount
                existing.roi = roi
                existing.sync_time = now
                updated += 1

        page_info = data.get("page_info") or {}
        total_page = int(page_info.get("total_page") or page)
        if page >= total_page or not items:
            break
        page += 1

    db.commit()
    return PlanReportSyncOut(
        advertiser_id=advertiser_id,
        start_date=start_date,
        end_date=end_date,
        total=total,
        created=created,
        updated=updated,
    )


async def update_roi(db: Session, payload: UpdateRoiIn) -> UpdateRoiOut:
    client = await token_store.build_client(db)
    ocean_data = await client.update_roi2_goal(
        payload.advertiser_id,
        payload.ad_id,
        payload.roi2_goal,
    )

    now = datetime.now().replace(microsecond=0)
    plan = db.scalar(
        select(QianchuanAdPlan).where(
            QianchuanAdPlan.advertiser_id == payload.advertiser_id,
            QianchuanAdPlan.ad_id == payload.ad_id,
        )
    )
    if plan is None:
        plan = QianchuanAdPlan(
            advertiser_id=payload.advertiser_id,
            ad_id=payload.ad_id,
            ad_name="",
            roi2_goal=_to_decimal(payload.roi2_goal),
            plan_status="",
            sync_time=now,
        )
        db.add(plan)
    else:
        plan.roi2_goal = _to_decimal(payload.roi2_goal)
        plan.sync_time = now
    db.commit()

    return UpdateRoiOut(
        advertiser_id=payload.advertiser_id,
        ad_id=payload.ad_id,
        roi2_goal=payload.roi2_goal,
        oceanengine=ocean_data if isinstance(ocean_data, dict) else {"raw": ocean_data},
    )
