from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy.orm import Session

from app.api.deps import get_db
from app.core.response import ok
from app.services import advertiser_dashboard as advertiser_dashboard_service
from app.services import advertiser_service, dashboard_cache
from app.services.account_access import current_username, is_admin

router = APIRouter(tags=["Advertiser"])


def _people_for(db: Session, username: str) -> list[str]:
    if is_admin(username):
        return advertiser_dashboard_service.list_people(db)
    return [username]


def _stamp(data: dict, username: str) -> dict:
    data["viewer"] = username
    data["viewer_scope"] = "全部人员" if is_admin(username) else username
    return data


@router.get("/advertiser/sync-all", summary="同步代理商下全部千川子账户")
async def sync_all_advertisers(request: Request, db: Session = Depends(get_db)):
    username = current_username(request)
    only_person = None if is_admin(username) else username
    data = await advertiser_service.sync_all_advertisers(db, only_person=only_person)
    return ok(data.model_dump())


@router.get(
    "/advertiser/dashboard",
    summary="子账户仪表盘（今天/7/30按人名走缓存；自定义直拉）",
)
async def get_advertiser_dashboard(
    request: Request,
    range: str | None = Query(
        None, description="today / 7 / 30；与自定义日期二选一"
    ),
    refresh: bool = Query(False, description="仅对 today/7/30 生效：按人名重拉"),
    start_date: str | None = Query(None, description="自定义开始 yyyy-MM-dd"),
    end_date: str | None = Query(None, description="自定义结束 yyyy-MM-dd"),
    db: Session = Depends(get_db),
):
    username = current_username(request)
    people = _people_for(db, username)
    range_key = (range or "").strip()
    if range_key in dashboard_cache.PRESET_RANGES:
        data = await dashboard_cache.fetch_people(db, range_key, people, force=refresh)
        return ok(_stamp(data, username))

    parts = []
    for person in people:
        part = await advertiser_dashboard_service.build_dashboard(
            db, start_date=start_date, end_date=end_date, person=person
        )
        part["from_cache"] = False
        parts.append(part)
    data = dashboard_cache.merge_dashboards(parts)
    data["range"] = "custom"
    return ok(_stamp(data, username))


@router.get(
    "/advertiser/dashboard/progress",
    summary="查询预设区间拉取进度（进度条用）",
)
async def dashboard_progress(
    range: str = Query(..., description="today / 7 / 30"),
):
    data = await dashboard_cache.get_progress(range)
    return ok(data)
