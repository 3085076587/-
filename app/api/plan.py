import asyncio
import logging
from datetime import date

from fastapi import APIRouter, Depends, File, Form, Query, Request, UploadFile
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.deps import get_db
from app.core.config import get_settings
from app.core.database import SessionLocal
from app.core.exceptions import AppError
from app.core.response import ok
from app.models.advertiser import QianchuanAdvertiser
from app.schemas.plan import (
    CreateUniPlanIn,
    DeletePlansIn,
    SaveCommissionIn,
    SaveProductRefundIn,
    UpdateRoiIn,
    UpdateStatusIn,
)
from app.clients import qianchuan_page
from app.services import account_scope, dashboard_cache, overview_service, plan_service, plugin_auth, product_refund, product_refund_api, today_plan_cache, uni_create_service, uni_plan_service, workbench_session
from app.services.account_access import assert_advertiser_access, assert_can_operate, current_username, visible_advertiser_ids

logger = logging.getLogger("uvicorn.error")
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
    if start > end:
        raise AppError("开始日期不能晚于结束日期")
    page_data = await _plans_from_qianchuan_page(db, advertiser_id, start, end)
    if page_data is not None:
        data = page_data
    elif start != today or end != today:
        data = await uni_plan_service.list_uni_plans(
            db, advertiser_id, start_date=start, end_date=end
        )
        await uni_plan_service.attach_product_stats(
            db, advertiser_id, start, end, data.get("plans") or []
        )
        product_refund.attach_refund_rates(db, advertiser_id, data.get("plans") or [])
        return ok(data)
    else:
        try:
            data = await uni_plan_service.list_uni_plans(
                db, advertiser_id, start_date=today, end_date=today
            )
        except Exception as exc:
            cached = await today_plan_cache.get_account(today, advertiser_id)
            if cached is None:
                raise
            cached["note"] = f"本次没拉到，仍显示该账户上一份计划（{exc}）"
            await uni_plan_service.attach_product_stats(
                db, advertiser_id, today, today, cached.get("plans") or []
            )
            product_refund.attach_refund_rates(db, advertiser_id, cached.get("plans") or [])
            return ok(cached)
        plans = list(data.get("plans") or [])
        await uni_plan_service.attach_product_stats(db, advertiser_id, today, today, plans)
        data["plans"] = plans
    plans = list(data.get("plans") or [])
    if start == today and end == today and data.get("source") == "qianchuan_page":
        saved = await today_plan_cache.put_account(
            today,
            advertiser_id,
            plans,
            start_date=today,
            end_date=today,
        )
        await overview_service.patch_today_account(db, advertiser_id, plans)
        data["cached_at"] = saved.get("cached_at")
        if not data.get("note"):
            data["note"] = "只更新了当前账户的计划"
    product_refund.attach_refund_rates(db, advertiser_id, plans)
    return ok(data)


def _visible_shops(db: Session, username: str) -> list[dict]:
    allowed = visible_advertiser_ids(db, username)
    stmt = select(QianchuanAdvertiser).order_by(QianchuanAdvertiser.advertiser_id.asc())
    agent = account_scope.agent_id()
    if agent:
        stmt = stmt.where(QianchuanAdvertiser.agent_id == int(agent))
    shops: list[dict] = []
    for row in db.scalars(stmt).all():
        name = row.advertiser_name or ""
        if not account_scope.keeps_name(name):
            continue
        adv_id = int(row.advertiser_id)
        if allowed is not None and adv_id not in allowed:
            continue
        shops.append(
            {
                "advertiser_id": adv_id,
                "advertiser_name": name,
                "person": account_scope.owner_label(name) or "",
            }
        )
    return shops


def _stamp_plans(plans: list[dict], shop: dict) -> list[dict]:
    for plan in plans:
        plan["advertiser_id"] = shop["advertiser_id"]
        plan["advertiser_name"] = shop["advertiser_name"]
        plan["person"] = shop["person"]
    return plans


async def _fetch_account_plans(advertiser_id: int, start: str, end: str, cookie: str) -> list[dict]:
    """一个账户的推商品计划。页面能拉就用页面，否则走开放平台，不拉乘方和直播。"""
    if cookie:
        try:
            return await qianchuan_page.fetch_product_plans(cookie, advertiser_id, start, end)
        except AppError as exc:
            if exc.code in (40121, 40421):
                raise
            logger.warning("千川页面计划没拉到，改走开放平台 advertiser=%s：%s", advertiser_id, exc.msg)
        except Exception as exc:  # noqa: BLE001
            logger.warning("千川页面计划没拉到，改走开放平台 advertiser=%s：%s", advertiser_id, exc)
    db = SessionLocal()
    try:
        data = await uni_plan_service.list_uni_plans(
            db,
            advertiser_id,
            start_date=start,
            end_date=end,
            sync_commission=False,
        )
        plans = list(data.get("plans") or [])
        await uni_plan_service.attach_product_stats(db, advertiser_id, start, end, plans)
        return plans
    finally:
        db.close()


@router.get("/plan/uni/board", summary="当前账号下全部账户的推商品计划")
async def plan_board(
    request: Request,
    start_date: str | None = Query(None, description="开始日期，默认今天"),
    end_date: str | None = Query(None, description="结束日期，默认今天"),
    refresh: int = Query(0, description="1 时今天也不读缓存，重新拉取"),
    db: Session = Depends(get_db),
):
    today = date.today().isoformat()
    start = start_date or today
    end = end_date or today
    if start > end:
        raise AppError("开始日期不能晚于结束日期")
    shops = _visible_shops(db, current_username(request))
    known = {int(shop["advertiser_id"]) for shop in shops}
    allowed = visible_advertiser_ids(db, current_username(request))
    for adv_id, name in (await dashboard_cache.today_workbench_shops()).items():
        if adv_id in known or not account_scope.keeps_name(name):
            continue
        if allowed is not None and adv_id not in allowed:
            continue
        shops.append(
            {
                "advertiser_id": adv_id,
                "advertiser_name": name,
                "person": account_scope.owner_label(name) or "",
            }
        )
    if not shops:
        return ok(
            {
                "plans": [],
                "total": 0,
                "accounts": 0,
                "cached_accounts": 0,
                "fetched_accounts": 0,
                "failed_accounts": 0,
                "start_date": start,
                "end_date": end,
                "note": "",
            }
        )
    use_cache = start == today and end == today and not refresh
    cached_map: dict[int, dict] = {}
    if use_cache:
        try:
            cached_map = await today_plan_cache.mget_accounts(today, [shop["advertiser_id"] for shop in shops])
        except Exception as exc:  # noqa: BLE001
            logger.warning("计划缓存没读到，改为现拉：%s", exc)
            cached_map = {}
    session = workbench_session.get_session(db)
    cookie = (session.cookie or "").strip() if session is not None else ""
    concurrency = max(1, int(get_settings().TODAY_PLAN_CONCURRENCY))
    gate = asyncio.Semaphore(concurrency)
    fetched: dict[int, list[dict]] = {}
    failed: list[int] = []
    fatal: AppError | None = None

    async def one(shop: dict) -> None:
        nonlocal fatal
        if fatal is not None:
            return
        adv_id = shop["advertiser_id"]
        if adv_id in cached_map and isinstance(cached_map[adv_id].get("plans"), list):
            return
        async with gate:
            if fatal is not None:
                return
            try:
                fetched[adv_id] = await _fetch_account_plans(adv_id, start, end, cookie)
            except AppError as exc:
                if exc.code in (40121, 40421):
                    fatal = exc
                    return
                failed.append(adv_id)
                logger.warning("账户 %s 计划没拉到：%s", adv_id, exc.msg)
            except Exception as exc:  # noqa: BLE001
                failed.append(adv_id)
                logger.warning("账户 %s 计划没拉到：%s", adv_id, exc)

    await asyncio.gather(*(one(shop) for shop in shops))
    if fatal is not None:
        raise fatal

    plans: list[dict] = []
    cached_accounts = 0
    fetched_accounts = 0
    for shop in shops:
        adv_id = shop["advertiser_id"]
        if adv_id in cached_map and isinstance(cached_map[adv_id].get("plans"), list):
            rows = list(cached_map[adv_id]["plans"])
            cached_accounts += 1
        elif adv_id in fetched:
            rows = fetched[adv_id]
            fetched_accounts += 1
            if start == today and end == today:
                try:
                    await today_plan_cache.put_account(
                        today,
                        adv_id,
                        rows,
                        start_date=today,
                        end_date=today,
                    )
                except Exception as exc:  # noqa: BLE001
                    logger.warning("账户 %s 计划缓存没写上：%s", adv_id, exc)
        else:
            continue
        uni_plan_service.apply_local_commission(db, adv_id, rows)
        product_refund.attach_refund_rates(db, adv_id, rows)
        plans.extend(_stamp_plans(rows, shop))

    notes: list[str] = []
    if cached_accounts and not fetched_accounts:
        notes.append("今天的计划来自缓存")
    elif cached_accounts:
        notes.append(f"{cached_accounts} 个账户用缓存，{fetched_accounts} 个账户现拉")
    if failed:
        notes.append(f"{len(failed)} 个账户这次没拉到")
    return ok(
        {
            "plans": plans,
            "total": len(plans),
            "accounts": len(shops),
            "cached_accounts": cached_accounts,
            "fetched_accounts": fetched_accounts,
            "failed_accounts": len(failed),
            "start_date": start,
            "end_date": end,
            "note": "；".join(notes),
        }
    )


async def _plans_from_qianchuan_page(db, advertiser_id: int, start: str, end: str):
    """千川页面能拉到就用页面。登录失效或接口失败时返回 None，改走开放平台。"""
    row = workbench_session.get_session(db)
    if row is None or not (row.cookie or "").strip():
        return None
    try:
        plans = await qianchuan_page.fetch_product_plans(row.cookie, advertiser_id, start, end)
    except AppError as exc:
        if exc.code in (40121, 40421):
            raise
        logger.warning("千川页面计划没拉到，改走开放平台 advertiser=%s：%s", advertiser_id, exc.msg)
        return None
    except Exception as exc:  # noqa: BLE001
        logger.warning("千川页面计划没拉到，改走开放平台 advertiser=%s：%s", advertiser_id, exc)
        return None
    uni_plan_service.apply_local_commission(db, advertiser_id, plans)
    return {
        "advertiser_id": advertiser_id,
        "start_date": start,
        "end_date": end,
        "plans": plans,
        "total": len(plans),
        "note": "千川页面",
        "source": "qianchuan_page",
    }


@router.get("/product/refund-rates", summary="商品退款率列表，页面用来算利润")
async def list_product_refund_rates(request: Request):
    current_username(request)
    rows = await product_refund_api.load_rates()
    data = []
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        product_id = str(row.get("product_id") or "").strip()
        if not product_id or product_id == "0":
            continue
        rate = row.get("refund_rate")
        if rate == "":
            rate = None
        data.append({"product_id": product_id, "refund_rate": rate})
    return ok(data)


@router.post("/product/refund-rate", summary="保存商品实际退款率")
async def save_product_refund(
    payload: SaveProductRefundIn,
    request: Request,
    db: Session = Depends(get_db),
):
    assert_advertiser_access(db, current_username(request), payload.advertiser_id)
    assert_can_operate(db, current_username(request), "plan")
    await plugin_auth.guard_plugin_write(
        request, payload.advertiser_id, product_id=payload.product_id
    )
    data = product_refund.save_product_refund(
        db,
        payload.advertiser_id,
        payload.product_id,
        payload.product_name,
        payload.actual_refund_rate,
    )
    today = date.today().isoformat()
    cached = await today_plan_cache.get_account(today, payload.advertiser_id)
    if cached is not None:
        plans = list(cached.get("plans") or [])
        for item in plans:
            if int(item.get("product_id") or 0) != int(payload.product_id):
                continue
            item["actual_refund_rate"] = data.get("actual_refund_rate")
        await today_plan_cache.put_account(
            today,
            payload.advertiser_id,
            plans,
            start_date=str(cached.get("start_date") or today),
            end_date=str(cached.get("end_date") or today),
        )
        await overview_service.patch_today_account(db, payload.advertiser_id, plans)
    return ok(data)


@router.post("/plan/commission", summary="保存计划佣金比例")
async def save_commission(payload: SaveCommissionIn, request: Request, db: Session = Depends(get_db)):
    assert_advertiser_access(db, current_username(request), payload.advertiser_id)
    assert_can_operate(db, current_username(request), "plan")
    await plugin_auth.guard_plugin_write(request, payload.advertiser_id, ad_id=payload.ad_id)
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
    assert_can_operate(db, current_username(request), "plan")
    data = await uni_plan_service.delete_plans(db, payload)
    return ok(data)


@router.post("/plan/update-status", summary="启停全域计划")
async def update_plan_status(payload: UpdateStatusIn, request: Request, db: Session = Depends(get_db)):
    assert_advertiser_access(db, current_username(request), payload.advertiser_id)
    assert_can_operate(db, current_username(request), "account")
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
    assert_can_operate(db, current_username(request), "plan")
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
    assert_can_operate(db, current_username(request), "plan")
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
    assert_can_operate(db, current_username(request), "plan")
    data = await uni_create_service.create_plan(db, payload)
    return ok(data)


@router.post("/plan/update-roi", summary="修改净成交 ROI 目标，可选提醒")
async def update_roi(payload: UpdateRoiIn, request: Request, db: Session = Depends(get_db)):
    assert_advertiser_access(db, current_username(request), payload.advertiser_id)
    assert_can_operate(db, current_username(request), "account")
    data = await uni_plan_service.update_roi_with_reminder(db, payload)
    return ok(data)
