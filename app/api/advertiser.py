from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.api.deps import get_db
from app.core.response import ok
from app.services import advertiser_dashboard as advertiser_dashboard_service
from app.services import advertiser_service, dashboard_cache

router = APIRouter(tags=["Advertiser"])


@router.get("/advertiser/sync-all", summary="同步代理商下全部千川子账户")
async def sync_all_advertisers(db: Session = Depends(get_db)):
    data = await advertiser_service.sync_all_advertisers(db)
    return ok(data.model_dump())


@router.get(
    "/advertiser/dashboard",
    summary="子账户仪表盘（今天/7/30走缓存；自定义直拉；refresh 清缓存）",
)
async def get_advertiser_dashboard(
    range: str | None = Query(
        None, description="today / 7 / 30；与自定义日期二选一"
    ),
    refresh: bool = Query(False, description="仅对 today/7/30 生效：删缓存后重拉"),
    start_date: str | None = Query(None, description="自定义开始 yyyy-MM-dd"),
    end_date: str | None = Query(None, description="自定义结束 yyyy-MM-dd"),
    db: Session = Depends(get_db),
):
    range_key = (range or "").strip()
    if range_key in dashboard_cache.PRESET_RANGES:
        # 删缓存在 fetch_and_cache 单飞内做，避免与启动预热并行打架
        data = await dashboard_cache.fetch_and_cache(db, range_key, force=refresh)
        return ok(data)

    # 自定义时间：不走缓存
    data = await advertiser_dashboard_service.build_dashboard(
        db, start_date=start_date, end_date=end_date
    )
    data["from_cache"] = False
    data["range"] = "custom"
    return ok(data)


@router.get(
    "/advertiser/dashboard/progress",
    summary="查询预设区间拉取进度（进度条用）",
)
async def dashboard_progress(
    range: str = Query(..., description="today / 7 / 30"),
):
    data = await dashboard_cache.get_progress(range)
    return ok(data)
