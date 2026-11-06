from datetime import date

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.core.exceptions import AppError
from app.core.response import ok
from app.services import overview_service, product_refund, product_report_service, today_plan_cache, uni_plan_service
from app.services.account_access import assert_advertiser_access, assert_can_operate, current_username
from app.services.plan_commission_sync import resolve_plan_commission_pct, user_saved_commissions
from app.services.plugin_page import parse_uni_html

router = APIRouter(tags=["Plugin"])


class PageHtmlIn(BaseModel):
    html: str = Field("", description="全域投放页当前 HTML")


@router.post("/plugin/page", summary="用 BeautifulSoup 解析全域投放页 HTML")
def parse_plugin_page(body: PageHtmlIn):
    return ok({"plans": parse_uni_html(body.html)})


class ProfitInputIn(BaseModel):
    advertiser_id: int
    ad_id: int
    product_id: int | None = None
    product_name: str = ""
    commission: str | None = Field(None, description="手填佣金百分数，空字符串表示改用名称")
    refund: str | None = Field(None, description="实际退款率百分数，空字符串表示未填")


def _parse_commission_input(raw: str | None) -> float | None:
    text = str(raw or "").replace("%", "").strip()
    if text == "":
        return None
    try:
        number = float(text)
    except ValueError as exc:
        raise AppError("佣金要填数字", code=40001) from exc
    if number <= 0 or number > 100:
        raise AppError("佣金要在 0 到 100 之间", code=40001)
    return round(number, 2)


@router.post("/plugin/profit-input", summary="插件同步手填的实际退款率和佣金")
async def sync_profit_input(
    payload: ProfitInputIn,
    request: Request,
    db: Session = Depends(get_db),
):
    assert_advertiser_access(db, current_username(request), payload.advertiser_id)
    assert_can_operate(db, current_username(request), "plan")
    commission = _parse_commission_input(payload.commission)
    today = date.today().isoformat()
    cached = await today_plan_cache.get_account(today, payload.advertiser_id)
    plans = list((cached or {}).get("plans") or [])
    matched = next((item for item in plans if int(item.get("ad_id") or 0) == int(payload.ad_id)), None)
    product_id = payload.product_id
    product_name = (payload.product_name or "").strip()
    if matched:
        if not product_id and matched.get("product_id"):
            product_id = int(matched["product_id"])
        if not product_name:
            product_name = str(matched.get("product_name") or matched.get("ad_name") or "")
    uni_plan_service.save_plan_commission(db, payload.advertiser_id, payload.ad_id, commission)
    if product_id:
        product_refund.save_product_refund(
            db,
            payload.advertiser_id,
            int(product_id),
            product_name,
            payload.refund,
        )
        product_report_service.save_advertiser_product_commission(
            db,
            payload.advertiser_id,
            int(product_id),
            product_name,
            commission,
        )
    if cached is not None:
        saved = user_saved_commissions(db, payload.advertiser_id)
        for item in plans:
            if int(item.get("ad_id") or 0) != int(payload.ad_id):
                continue
            item["commission_pct"] = resolve_plan_commission_pct(
                str(item.get("ad_name") or ""),
                int(payload.ad_id),
                saved,
            )
            item["actual_refund_rate"] = product_refund.percent_text(
                product_refund.parse_refund_input(payload.refund)
            )
        await today_plan_cache.put_account(
            today,
            payload.advertiser_id,
            plans,
            start_date=str(cached.get("start_date") or today),
            end_date=str(cached.get("end_date") or today),
        )
        await overview_service.patch_today_account(db, payload.advertiser_id, plans)
    return ok(
        {
            "advertiser_id": payload.advertiser_id,
            "ad_id": payload.ad_id,
            "product_id": product_id,
            "commission_pct": commission,
        }
    )
