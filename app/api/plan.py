from datetime import date

from fastapi import APIRouter, Depends, File, Form, Query, Request, UploadFile
from sqlalchemy.orm import Session

from app.api.deps import get_db
from app.core.exceptions import AppError
from app.core.response import ok
from app.schemas.plan import (
    CreateUniPlanIn,
    DeletePlansIn,
    SaveCommissionIn,
    SaveProductRefundIn,
    UpdateRoiIn,
    UpdateStatusIn,
)
from app.services import overview_service, plan_service, product_refund, today_plan_cache, uni_create_service, uni_plan_service
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
    today = date.today().isoformat()
    start = start_date or today
    end = end_date or today
    if start != today or end != today:
        data = await uni_plan_service.list_uni_plans(
            db, advertiser_id, start_date=start, end_date=end
        )
        product_refund.attach_refund_rates(db, advertiser_id, data.get("plans") or [])
        return ok(data)
    try:
        data = await uni_plan_service.list_uni_plans(
            db, advertiser_id, start_date=today, end_date=today
        )
    except Exception as exc:
        cached = await today_plan_cache.get_account(today, advertiser_id)
        if cached is None:
            raise
        cached["note"] = f"本次没拉到，仍显示该账户上一份计划（{exc}）"
        product_refund.attach_refund_rates(db, advertiser_id, cached.get("plans") or [])
        return ok(cached)
    plans = list(data.get("plans") or [])
    saved = await today_plan_cache.put_account(
        today,
        advertiser_id,
        plans,
        start_date=today,
        end_date=today,
    )
    await overview_service.patch_today_account(db, advertiser_id, plans)
    data["cached_at"] = saved.get("cached_at")
    data["note"] = "只更新了当前账户的计划"
    product_refund.attach_refund_rates(db, advertiser_id, data.get("plans") or [])
    return ok(data)


@router.post("/product/refund-rate", summary="保存商品实际退款率")
def save_product_refund(
    payload: SaveProductRefundIn,
    request: Request,
    db: Session = Depends(get_db),
):
    assert_advertiser_access(db, current_username(request), payload.advertiser_id)
    data = product_refund.save_product_refund(
        db,
        payload.advertiser_id,
        payload.product_id,
        payload.product_name,
        payload.actual_refund_rate,
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
    today = date.today().isoformat()
    cached = await today_plan_cache.get_account(today, payload.advertiser_id)
    if cached is not None:
        plans = list(cached.get("plans") or [])
        for item in plans:
            if int(item.get("ad_id") or 0) != int(payload.ad_id):
                continue
            item["commission_pct"] = uni_plan_service.plan_commission_sync.resolve_plan_commission_pct(
                str(item.get("ad_name") or ""),
                int(payload.ad_id),
                {int(payload.ad_id): float(data["commission_pct"])} if data.get("commission_pct") else {},
            )
        await today_plan_cache.put_account(
            today,
            payload.advertiser_id,
            plans,
            start_date=str(cached.get("start_date") or today),
            end_date=str(cached.get("end_date") or today),
        )
        await overview_service.patch_today_account(db, payload.advertiser_id, plans)
    return ok(data)


@router.get("/plan/uni/logs", summary="全域商品计划操作日志")
async def list_uni_plan_logs(
    request: Request,
    advertiser_id: int = Query(..., description="千川子账户 ID"),
    ad_id: str = Query(..., description="计划 ID"),
    start_date: str = Query(..., description="开始日期 yyyy-mm-dd"),
    end_date: str = Query(..., description="结束日期 yyyy-mm-dd"),
    page: int = Query(1, ge=1),
    db: Session = Depends(get_db),
):
    assert_advertiser_access(db, current_username(request), advertiser_id)
    try:
        plan_id = int(str(ad_id).strip())
    except ValueError as exc:
        raise AppError("计划 ID 不正确") from exc
    data = await uni_plan_service.list_plan_logs(
        db, advertiser_id, plan_id, start_date, end_date, page
    )
    return ok(data)


@router.post("/plan/uni/delete", summary="删除推商品全域计划")
async def delete_uni_plans(payload: DeletePlansIn, request: Request, db: Session = Depends(get_db)):
    assert_advertiser_access(db, current_username(request), payload.advertiser_id)
    data = await uni_plan_service.delete_plans(db, payload)
    return ok(data)


@router.post("/plan/update-status", summary="启停全域计划")
async def update_plan_status(payload: UpdateStatusIn, request: Request, db: Session = Depends(get_db)):
    assert_advertiser_access(db, current_username(request), payload.advertiser_id)
    data = await uni_plan_service.update_plan_status(db, payload)
    return ok(data)


@router.get("/plan/uni/awemes", summary="商品全域可投抖音号")
async def list_uni_awemes(
    request: Request,
    advertiser_id: int = Query(..., description="千川子账户 ID"),
    db: Session = Depends(get_db),
):
    assert_advertiser_access(db, current_username(request), advertiser_id)
    rows = await uni_create_service.list_awemes(db, advertiser_id)
    return ok(rows)


@router.get("/plan/uni/products", summary="商品全域可选商品")
async def list_uni_products(
    request: Request,
    advertiser_id: int = Query(..., description="千川子账户 ID"),
    aweme_id: int | None = Query(None, description="有号商家传抖音号，无号不传"),
    keyword: str = Query("", description="商品名"),
    page: int = Query(1, ge=1),
    db: Session = Depends(get_db),
):
    assert_advertiser_access(db, current_username(request), advertiser_id)
    data = await uni_create_service.list_products(
        db, advertiser_id, aweme_id=aweme_id, keyword=keyword, page=page
    )
    return ok(data)


@router.post("/plan/uni/upload-video", summary="上传商品全域视频素材")
async def upload_uni_video(
    request: Request,
    advertiser_id: int = Form(...),
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
):
    assert_advertiser_access(db, current_username(request), advertiser_id)
    content = await file.read()
    data = await uni_create_service.upload_video(
        db, advertiser_id, file.filename or "video.mp4", content
    )
    return ok(data)


@router.post("/plan/uni/upload-image", summary="上传商品全域图片素材")
async def upload_uni_image(
    request: Request,
    advertiser_id: int = Form(...),
    kind: str = Form("card"),
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
):
    assert_advertiser_access(db, current_username(request), advertiser_id)
    content = await file.read()
    data = await uni_create_service.upload_image(
        db,
        advertiser_id,
        file.filename or "image.jpg",
        content,
        kind="cover" if kind == "cover" else "card",
    )
    return ok(data)


@router.post("/plan/uni/create", summary="新建商品全域计划")
async def create_uni_plan(payload: CreateUniPlanIn, request: Request, db: Session = Depends(get_db)):
    assert_advertiser_access(db, current_username(request), payload.advertiser_id)
    data = await uni_create_service.create_plan(db, payload)
    return ok(data)


@router.post("/plan/update-roi", summary="修改净成交 ROI 目标，可选提醒")
async def update_roi(payload: UpdateRoiIn, request: Request, db: Session = Depends(get_db)):
    assert_advertiser_access(db, current_username(request), payload.advertiser_id)
    data = await uni_plan_service.update_roi_with_reminder(db, payload)
    return ok(data)
