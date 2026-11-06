"""千川投放页的推商品计划列表。走页面自己的接口，不走巨量开放平台。"""

from __future__ import annotations

import logging
from typing import Any

import httpx

from app.clients.workbench import parse_cookie
from app.core.exceptions import AppError

logger = logging.getLogger("uvicorn.error")

_ORIGIN = "https://qianchuan.jinritemai.com"
_LIST_URL = f"{_ORIGIN}/ad/api/pmc/v1/uni-promotion/ad/list-required"
_PAGE_SIZE = 100
_METRICS = [
    "stat_cost_for_roi2",
    "total_pay_order_count_for_roi2",
    "total_pay_order_gmv_for_roi2",
    "total_prepay_and_pay_order_roi2",
    "total_order_settle_amount_for_roi2_1h",
    "total_prepay_and_pay_settle_roi2_1h",
    "total_refund_order_gmv_for_roi2_1h_rate",
]
_DEEP_ACTION = {
    576: "AD_CONVERT_TYPE_LIVE_PURE_PAY_ROI",
    326: "AD_CONVERT_TYPE_LIVE_PAY_ROI",
}


def _num(value: Any) -> float | None:
    if isinstance(value, dict):
        value = value.get("value")
    if value in (None, ""):
        return None
    text = str(value).strip().replace(",", "")
    if not text or text in {"-", "--"}:
        return None
    try:
        return float(text)
    except ValueError:
        return None


def _metric(metrics: dict[str, Any], key: str) -> tuple[float | None, str]:
    raw = metrics.get(key)
    text = ""
    if isinstance(raw, dict):
        text = str(raw.get("valueStr") or "")
    return _num(raw), text


def _count(metrics: dict[str, Any], key: str) -> int:
    number, _text = _metric(metrics, key)
    if number is None:
        return 0
    return int(round(number))


def _money(metrics: dict[str, Any], key: str) -> float:
    number, _text = _metric(metrics, key)
    if number is None:
        return 0.0
    return round(number, 2)


def _ratio(metrics: dict[str, Any], key: str) -> float | None:
    number, _text = _metric(metrics, key)
    if number is None:
        return None
    return round(number, 4)


def _rate(metrics: dict[str, Any], key: str) -> float | None:
    """页面退款率「1.24%」的 value 是 1.24，这里换成 0~1。"""
    number, text = _metric(metrics, key)
    if number is None:
        return None
    if text.endswith("%") or number > 1:
        number = number / 100.0
    if number < 0 or number > 1:
        return None
    return round(number, 6)


def _budget_yuan(value: Any) -> float | None:
    number = _num(value)
    if number is None:
        return None
    return round(number / 100000.0, 2)


def _enabled(ad: dict[str, Any]) -> bool:
    label = str(ad.get("adDeliveryName") or "")
    if "暂停" in label or "删除" in label or "终止" in label:
        return False
    try:
        return int(ad.get("adOptType") or 0) == 0
    except (TypeError, ValueError):
        return True


def _deep_action(ad: dict[str, Any]) -> str:
    raw = ad.get("deepExternalAction")
    try:
        code = int(raw)
    except (TypeError, ValueError):
        code = 0
    if code in _DEEP_ACTION:
        return _DEEP_ACTION[code]
    name = str(ad.get("deepExternalActionName") or "")
    if "净成交" in name:
        return "AD_CONVERT_TYPE_LIVE_PURE_PAY_ROI"
    return "AD_CONVERT_TYPE_LIVE_PURE_PAY_ROI"


def _plan(advertiser_id: int, ad: dict[str, Any], goods: list[Any], metrics: dict[str, Any]) -> dict[str, Any]:
    product_ids: list[int] = []
    product_name = ""
    product_image = ""
    for item in goods:
        if not isinstance(item, dict):
            continue
        if not product_name:
            product_name = str(item.get("name") or "")
            product_image = str(item.get("mainImageUrl") or item.get("image") or "")
        try:
            pid = int(item.get("id") or 0)
        except (TypeError, ValueError):
            pid = 0
        if pid and pid not in product_ids:
            product_ids.append(pid)
    enabled = _enabled(ad)
    try:
        ad_id = int(ad.get("id") or 0)
    except (TypeError, ValueError):
        ad_id = 0
    return {
        "advertiser_id": advertiser_id,
        "ad_id": ad_id,
        "ad_name": str(ad.get("name") or ""),
        "product_id": product_ids[0] if product_ids else None,
        "product_ids": product_ids,
        "product_name": product_name,
        "product_image": product_image,
        "opt_status": "ENABLE" if enabled else "DISABLE",
        "status": str(ad.get("adDeliveryType") or ""),
        "status_label": str(ad.get("adDeliveryName") or "-"),
        "enabled": enabled,
        "budget": _budget_yuan(ad.get("budget")),
        "budget_mode": ad.get("budgetMode"),
        "roi2_goal": ad.get("ecpRoi2Goal"),
        "deep_external_action": _deep_action(ad),
        "stat_cost": _money(metrics, "statCostForRoi2"),
        "pay_amount": _money(metrics, "totalPayOrderGmvForRoi2"),
        "pay_roi": _ratio(metrics, "totalPrepayAndPayOrderRoi2"),
        "settle_amount": _money(metrics, "totalOrderSettleAmountForRoi21H"),
        "settle_roi": _ratio(metrics, "totalPrepayAndPaySettleRoi21H"),
        "refund_rate_1h": _rate(metrics, "totalRefundOrderGmvForRoi21HRate"),
        "order_count": _count(metrics, "totalPayOrderCountForRoi2"),
        "stats_from_page": True,
    }


def _headers(cookie: str, advertiser_id: int) -> dict[str, str]:
    jar = parse_cookie(cookie)
    csrf = jar.get("csrftoken") or jar.get("passport_csrf_token") or ""
    headers = {
        "accept": "application/json, text/plain, */*",
        "content-type": "application/json",
        "origin": _ORIGIN,
        "referer": f"{_ORIGIN}/uni-prom?aavid={advertiser_id}",
        "user-agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        ),
        "cookie": cookie,
    }
    if csrf:
        headers["x-csrftoken"] = csrf
    return headers


def _body(start: str, end: str, page: int) -> dict[str, Any]:
    return {
        "UseNewChain": True,
        "Params": {
            "AdFilter": {
                "AdlabScene": 0,
                "MarGoal": 1,
                "UniPromShopFilter": {"UniPromShopView": 1},
                "NotInEcpAdStatuses": ["delete", "frozen"],
                "DataTimeRange": {
                    "StartTime": f"{start} 00:00:00",
                    "EndTime": f"{end} 23:59:59",
                },
            },
            "Metrics": list(_METRICS),
            "OrderBy": {"Type": 2, "Field": "stat_cost_for_roi2"},
            "NeedRequestOptional": True,
            "ListAdsModules": [10, 4, 20, 36, 7, 8],
            "SophonxDataSetKey": "product_roi2_promotion",
            "PageParams": {"Page": page, "PageSize": _PAGE_SIZE},
        },
    }


async def fetch_product_plans(
    cookie: str,
    advertiser_id: int,
    start: str,
    end: str,
) -> list[dict[str, Any]]:
    """按日期拉取一个店的推商品计划。金额已经是元。"""
    if not (cookie or "").strip():
        raise AppError("工作台登录还没有保存，无法从千川页面拉取计划", code=40421)
    plans: list[dict[str, Any]] = []
    seen: set[int] = set()
    async with httpx.AsyncClient(timeout=30.0, follow_redirects=False) as client:
        page = 1
        while page <= 20:
            response = await client.post(
                _LIST_URL,
                params={"aavid": str(advertiser_id)},
                headers=_headers(cookie, advertiser_id),
                json=_body(start, end, page),
            )
            if response.status_code in {301, 302, 401, 403} or "text/html" in response.headers.get("content-type", ""):
                raise AppError("千川页面登录已失效，需要重新登录工作台", code=40121)
            try:
                payload = response.json()
            except ValueError as exc:
                raise AppError("千川计划页返回的不是 JSON", code=50221) from exc
            if payload.get("status_code", 0) not in (0, "0"):
                message = str(payload.get("message") or "千川计划页请求失败")
                raise AppError(message, code=50221)
            data = payload.get("data") or {}
            ads = data.get("adInfos") or []
            goods_map = data.get("adGoodsMap") or {}
            stats_map = data.get("adStatsMap") or {}
            if not isinstance(ads, list):
                break
            for ad in ads:
                if not isinstance(ad, dict):
                    continue
                ad_key = str(ad.get("id") or "")
                try:
                    ad_id = int(ad_key or 0)
                except ValueError:
                    continue
                if not ad_id or ad_id in seen:
                    continue
                seen.add(ad_id)
                goods = goods_map.get(ad_key) or goods_map.get(ad_id) or []
                stats = stats_map.get(ad_key) or stats_map.get(ad_id) or {}
                metrics = stats.get("metrics") if isinstance(stats, dict) else {}
                plans.append(_plan(advertiser_id, ad, goods if isinstance(goods, list) else [], metrics or {}))
            pagination = data.get("pagination") or {}
            try:
                total_page = int(pagination.get("totalPage") or 1)
            except (TypeError, ValueError):
                total_page = 1
            logger.info(
                "千川计划页 账户 %s %s~%s 第 %s/%s 页 %s 条",
                advertiser_id,
                start,
                end,
                page,
                total_page,
                len(ads),
            )
            if page >= total_page or len(ads) < _PAGE_SIZE:
                break
            page += 1
    return plans
