"""数据总览接口。"""

from datetime import date

from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.api.deps import get_db
from app.core.exceptions import AppError
from app.core.response import ok
from app.services import overview_service, product_report_service
from app.services.account_access import current_username, is_admin, visible_advertiser_ids
from app.services.dashboard_cache import get_progress

router = APIRouter(tags=["Overview"])


class ProductCommissionIn(BaseModel):
    product_id: int
    commission_pct: float = Field(gt=0, le=100)


@router.get("/overview/summary", summary="数据总览（消耗/利润/产品Top）")
async def overview_summary(
    request: Request,
    start_date: str | None = Query(None, description="开始 yyyy-MM-dd，默认今天"),
    end_date: str | None = Query(None, description="结束 yyyy-MM-dd，默认今天"),
    person: str | None = Query(None, description="运营姓名，仅 admin 可指定"),
    refresh: bool = Query(False, description="忽略缓存强制重拉"),
    db: Session = Depends(get_db),
):
    username = current_username(request)
    today = date.today().isoformat()
    start = start_date or today
    end = end_date or today
    if is_admin(username):
        people = [person] if person else None
    else:
        people = [username]
    data = await overview_service.build_overview(db, start, end, people, force=refresh)
    return ok(data)


@router.get("/overview/compare", summary="两个授权账号的总览和合计")
async def overview_compare(
    request: Request,
    start_date: str | None = Query(None, description="开始 yyyy-MM-dd，默认今天"),
    end_date: str | None = Query(None, description="结束 yyyy-MM-dd，默认今天"),
    db: Session = Depends(get_db),
):
    username = current_username(request)
    if not is_admin(username):
        raise AppError("只有管理员可以看账号合计", code=40301)
    today = date.today().isoformat()
    start = start_date or today
    end = end_date or today
    try:
        date.fromisoformat(start)
        date.fromisoformat(end)
    except ValueError as exc:
        raise AppError("日期格式应为 yyyy-MM-dd", code=40001) from exc
    if start > end:
        raise AppError("开始日期不能晚于结束日期", code=40001)
    data = await overview_service.build_accounts_compare(db, start, end)
    return ok(data)


@router.get("/overview/progress", summary="总览拉取进度")
async def overview_progress(key: str = Query(..., description="start~end")):
    data = await get_progress(key)
    return ok(data)


@router.post("/overview/product-commission", summary="设置商品佣金（默认 60%）")
async def set_product_commission(
    request: Request,
    payload: ProductCommissionIn,
    db: Session = Depends(get_db),
):
    username = current_username(request)
    if not is_admin(username):
        raise AppError("仅管理员可修改商品佣金", code=40301)
    hit = product_report_service.set_product_commission(
        db, payload.product_id, payload.commission_pct
    )
    if not hit:
        raise AppError("商品还未入库，先加载一次总览数据", code=40401)
    await overview_service.apply_today_commission(payload.product_id, payload.commission_pct)
    return ok({"product_id": payload.product_id, "commission_pct": payload.commission_pct})
