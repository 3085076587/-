from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy.orm import Session

from app.api.deps import get_db
from app.core.response import ok
from app.schemas.plan import SaveCommissionIn, UpdateRoiIn, UpdateStatusIn
from app.services import plan_service, uni_plan_service
from app.services.account_access import assert_advertiser_access, current_username

router = APIRouter(tags=["Plan"])


@router.get("/plan/sync", summary="同步单个子账户下的计划")
async def sync_plans(
    request: Request,
    advertiser_id: int = Query(..., description="千川子账户 ID"),
    db: Session = Depends(get_db),
):
    assert_advertiser_access(db, current_username(request), advertiser_id)
    data = await plan_service.sync_plans(db, advertiser_id)
    return ok(data.model_dump())


@router.get("/plan/report/sync", summary="同步计划报表数据")
async def sync_plan_reports(
    request: Request,
    advertiser_id: int = Query(..., description="千川子账户 ID"),
    start_date: str = Query(..., description="开始日期 yyyy-MM-dd"),
    end_date: str = Query(..., description="结束日期 yyyy-MM-dd"),
    db: Session = Depends(get_db),
):
    assert_advertiser_access(db, current_username(request), advertiser_id)
    data = await plan_service.sync_plan_reports(db, advertiser_id, start_date, end_date)
    return ok(data.model_dump())


@router.get("/plan/uni/list", summary="实时拉取全域商品计划列表（图二）")
async def list_uni_plans(
    request: Request,
    advertiser_id: int = Query(..., description="千川子账户 ID"),
    start_date: str | None = Query(None, description="开始日期，默认今天"),
    end_date: str | None = Query(None, description="结束日期，默认今天"),
    db: Session = Depends(get_db),
):
    assert_advertiser_access(db, current_username(request), advertiser_id)
    data = await uni_plan_service.list_uni_plans(
        db, advertiser_id, start_date=start_date, end_date=end_date
    )
    return ok(data)


@router.post("/plan/commission", summary="保存计划佣金比例")
async def save_commission(payload: SaveCommissionIn, request: Request, db: Session = Depends(get_db)):
    assert_advertiser_access(db, current_username(request), payload.advertiser_id)
    data = uni_plan_service.save_plan_commission(
        db,
        payload.advertiser_id,
        payload.ad_id,
        payload.commission_pct,
    )
    return ok(data)


@router.post("/plan/update-status", summary="启停全域计划")
async def update_plan_status(payload: UpdateStatusIn, request: Request, db: Session = Depends(get_db)):
    assert_advertiser_access(db, current_username(request), payload.advertiser_id)
    data = await uni_plan_service.update_plan_status(db, payload)
    return ok(data)


@router.post("/plan/update-roi", summary="修改净成交 ROI 目标，可选提醒")
async def update_roi(payload: UpdateRoiIn, request: Request, db: Session = Depends(get_db)):
    assert_advertiser_access(db, current_username(request), payload.advertiser_id)
    data = await uni_plan_service.update_roi_with_reminder(db, payload)
    return ok(data)
