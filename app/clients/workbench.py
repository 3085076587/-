"""巨量引擎工作台内部账户表。只给子账户页用，不走开放平台。"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx

from app.core.exceptions import AppError

logger = logging.getLogger("uvicorn.error")

_SHANGHAI = timezone(timedelta(hours=8))
_ORIGIN = "https://business.oceanengine.com"
_PAGE_SIZE = 100
_MAX_PAGES = 20
# 商品投放账户表，和页面上看到的列一致。
_FIELDS = [
    "stat_cost_for_roi2",
    "total_prepay_and_pay_order_roi2",
    "total_pay_order_gmv_for_roi2",
    "total_order_settle_amount_for_roi2_1h",
    "total_prepay_and_pay_settle_roi2_1h",
    "total_refund_order_gmv_for_roi2_1h_rate",
]
_REFER = "ebp,7460465270428123145,7471055054527807526,ebp_promotion_ecp_roi2_product_account"


def _unix_range(start_date: str, end_date: str) -> tuple[str, str]:
    start = datetime.strptime(start_date, "%Y-%m-%d").replace(tzinfo=_SHANGHAI)
    end = datetime.strptime(end_date, "%Y-%m-%d").replace(tzinfo=_SHANGHAI) + timedelta(days=1)
    return str(int(start.timestamp())), str(int(end.timestamp()))


def parse_cookie(raw: str) -> dict[str, str]:
    jar: dict[str, str] = {}
    for part in (raw or "").split(";"):
        if "=" not in part:
            continue
        key, value = part.split("=", 1)
        key = key.strip()
        if key:
            jar[key] = value.strip()
    return jar


def cookie_header(jar: dict[str, str]) -> str:
    return "; ".join(f"{key}={value}" for key, value in jar.items())


def merge_response_cookies(raw: str, response: httpx.Response) -> tuple[str, str]:
    jar = parse_cookie(raw)
    for cookie in response.cookies.jar:
        if cookie.name:
            jar[cookie.name] = cookie.value or ""
    csrf = jar.get("csrftoken") or ""
    return cookie_header(jar), csrf


def _money(value: Any) -> float:
    text = str(value or "0").strip().replace(",", "")
    if not text or text in {"-", "--"}:
        return 0.0
    try:
        return round(float(text), 2)
    except ValueError:
        return 0.0


def _roi(value: Any) -> float | None:
    text = str(value or "").strip().replace(",", "")
    if not text or text in {"-", "--"}:
        return None
    try:
        return round(float(text), 4)
    except ValueError:
        return None


def _rate(value: Any) -> float | None:
    """工作台退款率是「10.21%」。页面会再加 5 个百分点，这里先换成 0~1。"""
    text = str(value or "").strip().replace(",", "")
    if not text or text in {"-", "--"}:
        return None
    percent = text.endswith("%")
    if percent:
        text = text[:-1]
    try:
        number = float(text)
    except ValueError:
        return None
    if percent or number > 1:
        number = number / 100.0
    if number < 0 or number > 1:
        return None
    return round(number, 6)


def _account(item: dict[str, Any]) -> dict[str, Any] | None:
    adv_id = int(item.get("advertiser_id") or 0)
    if not adv_id:
        return None
    metrics = item.get("metrics") or {}
    return {
        "advertiser_id": adv_id,
        "advertiser_name": str(item.get("advertiser_name") or ""),
        "status": str(item.get("advertiser_status_name") or "-"),
        "stat_cost": _money(metrics.get("stat_cost_for_roi2")),
        "pay_amount": _money(metrics.get("total_pay_order_gmv_for_roi2")),
        "pay_roi": _roi(metrics.get("total_prepay_and_pay_order_roi2")),
        "settle_amount": _money(metrics.get("total_order_settle_amount_for_roi2_1h")),
        "settle_roi": _roi(metrics.get("total_prepay_and_pay_settle_roi2_1h")),
        "refund_rate_1h": _rate(metrics.get("total_refund_order_gmv_for_roi2_1h_rate")),
        "plan_count": 0,
        "commission_base": 0.0,
    }


async def fetch_product_accounts(
    *,
    ebpid: int,
    cookie: str,
    csrf: str,
    start_date: str,
    end_date: str,
    page_size: int = _PAGE_SIZE,
    max_pages: int = _MAX_PAGES,
) -> tuple[list[dict[str, Any]], str, str]:
    """拉取商品投放账户表。返回账户行、更新后的 Cookie、csrf。"""
    start_time, end_time = _unix_range(start_date, end_date)
    url = f"{_ORIGIN}/api/ebp/promotion/ecp/get_roi2_account_list"
    headers = {
        "accept": "application/json",
        "content-type": "application/json",
        "origin": _ORIGIN,
        "referer": f"{_ORIGIN}/ebp/account-manage/ecp/bidding/universe/account?ebpid={ebpid}",
        "user-agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        ),
        "x-csrftoken": csrf or parse_cookie(cookie).get("csrftoken", ""),
        "cookie": cookie,
    }
    accounts: list[dict[str, Any]] = []
    current_cookie = cookie
    current_csrf = headers["x-csrftoken"]
    async with httpx.AsyncClient(timeout=30.0, follow_redirects=False) as client:
        for page in range(1, max_pages + 1):
            body = {
                "startTime": start_time,
                "endTime": end_time,
                "cascadeMetrics": ["advertiser_name", "advertiser_id", "advertiser_status"],
                "fields": list(_FIELDS),
                "orderField": "stat_cost_for_roi2",
                "orderType": 1,
                "offset": page,
                "limit": page_size,
                "accountType": 80,
                "filter": {
                    "pricingCategory": [2],
                    "campaign": {"marketingGoal": ["1"]},
                    "advertiser": {},
                },
                "refer": _REFER,
            }
            response = await client.post(url, params={"ebpid": str(ebpid)}, headers=headers, content=json.dumps(body))
            current_cookie, current_csrf = merge_response_cookies(current_cookie, response)
            headers["cookie"] = current_cookie
            if current_csrf:
                headers["x-csrftoken"] = current_csrf
            if response.status_code in {301, 302, 401, 403} or "text/html" in response.headers.get("content-type", ""):
                raise AppError("工作台登录已失效，需要重新登录后再拉子账户", code=40121)
            try:
                payload = response.json()
            except ValueError as exc:
                raise AppError("工作台账户表返回的不是 JSON", code=50221) from exc
            if payload.get("code") not in (0, "0"):
                message = str(payload.get("msg") or payload.get("message") or "工作台账户表请求失败")
                raise AppError(message, code=50221)
            data = payload.get("data") or {}
            chunk = data.get("list") or []
            for item in chunk:
                if isinstance(item, dict):
                    account = _account(item)
                    if account is not None:
                        accounts.append(account)
            pagination = data.get("pagination") or {}
            has_more = bool(pagination.get("hasMore"))
            logger.info(
                "工作台账户表第 %s 页 %s 条，合计 %s",
                page,
                len(chunk),
                pagination.get("total"),
            )
            if not has_more or len(chunk) < page_size:
                break
    return accounts, current_cookie, current_csrf
