from datetime import date

from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy.orm import Session

from app.api.deps import get_db
from app.core.response import ok
from app.services import advertiser_dashboard as advertiser_dashboard_service
from app.services import advertiser_service, dashboard_cache, overview_service
from app.services.account_access import current_username, is_admin, people_for_visible, visible_advertiser_ids

router = APIRouter(tags=["Advertiser"])


def _people_for(db: Session, username: str) -> list[str]:
    if is_admin(username):
        return advertiser_dashboard_service.list_people(db)
    return [username]


def _empty_dashboard() -> dict:
    return {
        "start_date": "",
        "end_date": "",
        "overview": {
            "stat_cost": 0.0,
            "pay_amount": 0.0,
            "pay_roi": None,
            "settle_amount": 0.0,
            "settle_roi": None,
        },
        "accounts": [],
        "total": 0,
        "stale_count": 0,
        "from_cache": True,
        "cached_at": None,
    }


def _keep_visible_accounts(data: dict, allowed: set[int] | None) -> None:
    if allowed is None:
        return
    accounts = [
        item
        for item in (data.get("accounts") or [])
        if int(item.get("advertiser_id") or 0) in allowed
    ]
    accounts.sort(key=lambda item: float(item.get("stat_cost") or 0), reverse=True)
    data["accounts"] = accounts
    data["total"] = len(accounts)
    sum_cost = sum(float(item.get("stat_cost") or 0) for item in accounts)
    sum_pay = sum(float(item.get("pay_amount") or 0) for item in accounts)
    sum_settle = sum(float(item.get("settle_amount") or 0) for item in accounts)
    data["overview"] = {
        "stat_cost": round(sum_cost, 2),
        "pay_amount": round(sum_pay, 2),
        "pay_roi": advertiser_dashboard_service.weighted_pay_roi(accounts),
        "settle_amount": round(sum_settle, 2),
        "settle_roi": advertiser_dashboard_service.weighted_settle_roi(accounts),
    }


def _stamp(data: dict, username: str) -> dict:
    data["viewer"] = username
    data["viewer_scope"] = "全部人员" if is_admin(username) else username
    return data


async def _attach_profits(db: Session, data: dict) -> dict:
    """利润不进工作台缓存。昨天及以前读商品日表，佣金用当前计划上的手填或名称；今天用计划缓存。"""
    start = data.get("start_date") or date.today().isoformat()
    end = data.get("end_date") or start
    profits, note = await overview_service.shop_profits(db, start, end)
    data["profit_note"] = note
    for acc in data.get("accounts") or []:
        adv_id = int(acc.get("advertiser_id") or 0)
        acc["profit"] = None if profits is None else profits.get(adv_id, 0.0)
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
    allowed = visible_advertiser_ids(db, username)
    if allowed is not None:
        people = people_for_visible(db, allowed)
    range_key = (range or "").strip()
    if range_key in dashboard_cache.PRESET_RANGES:
        if allowed is not None and not people:
            data = _empty_dashboard()
        else:
            data = await dashboard_cache.fetch_people(
                db, range_key, people, force=refresh, scope_all=is_admin(username)
            )
        _keep_visible_accounts(data, allowed)
        await _attach_profits(db, data)
        return ok(_stamp(data, username))

    today = date.today().isoformat()
    start = start_date or today
    end = end_date or today
    if is_admin(username):
        rows = await advertiser_dashboard_service.load_roi2_accounts(start, end)
        discovered = advertiser_dashboard_service.people_from_accounts(rows)
        if discovered:
            people = discovered
    parts = []
    for person in people:
        part = await advertiser_dashboard_service.build_dashboard(
            db, start_date=start, end_date=end, person=person
        )
        part["from_cache"] = False
        parts.append(part)
    data = dashboard_cache.merge_dashboards(parts)
    data["range"] = "custom"
    _keep_visible_accounts(data, allowed)
    await _attach_profits(db, data)
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
