"""子账户页：并发拉取商品全域消耗。"""

from __future__ import annotations

import asyncio
import logging
from datetime import date
from typing import Any, Awaitable, Callable

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.clients.oceanengine import OceanEngineClient
from app.core.config import get_settings
from app.core.exceptions import OceanEngineApiError
from app.models.advertiser import QianchuanAdvertiser
from app.services import token_store

logger = logging.getLogger("uvicorn.error")

ProgressCb = Callable[[int, int, str], Awaitable[None]] | None

FIELDS = [
    "stat_cost",
    "total_pay_order_gmv_for_roi2",
    "total_prepay_and_pay_order_roi2",
    "total_order_settle_amount_for_roi2_1h",
    "total_prepay_and_pay_settle_roi2_1h",
    "total_refund_order_gmv_for_roi2_1h_rate",
    "total_refund_order_gmv_for_roi2_1h_all",
]


def _num(value: Any, default: float = 0.0) -> float:
    try:
        return float(value) if value not in (None, "") else default
    except (TypeError, ValueError):
        return default


def _mf_to_yuan(value: Any) -> float:
    return round(_num(value) / 100000.0, 2)


def _norm_rate(value: Any) -> float | None:
    """退款率统一成 0~1。接口有时给比值 0.0644，有时给百分数 6.44。"""
    rate = _num(value, default=-1)
    if rate < 0:
        return None
    while rate > 1 and rate <= 10000:
        rate = rate / 100.0
    if rate > 1:
        return None
    return round(rate, 6)


def _find_metric(node: Any, key: str) -> Any:
    if isinstance(node, dict):
        if key in node and node[key] not in (None, ""):
            return node[key]
        for value in node.values():
            found = _find_metric(value, key)
            if found is not None:
                return found
    elif isinstance(node, list):
        for value in node:
            found = _find_metric(value, key)
            if found is not None:
                return found
    return None


async def _account_refund_rate(
    client: OceanEngineClient,
    advertiser_id: int,
    start_date: str,
    end_date: str,
) -> float | None:
    """计划列表没有退款率时，用账户维度全域报表补一次。"""
    try:
        report = await client.get_uni_promotion_report(
            advertiser_id,
            start_date,
            end_date,
            fields=[
                "total_refund_order_gmv_for_roi2_1h_rate",
                "total_refund_order_gmv_for_roi2_1h_all",
                "total_pay_order_gmv_for_roi2",
            ],
            marketing_goal="VIDEO_PROM_GOODS",
        )
    except OceanEngineApiError as exc:
        logger.info("账户退款率拉取失败 advertiser_id=%s: %s", advertiser_id, exc)
        return None
    rate = _norm_rate(_find_metric(report, "total_refund_order_gmv_for_roi2_1h_rate"))
    if rate is not None:
        return rate
    refund_gmv = _num(_find_metric(report, "total_refund_order_gmv_for_roi2_1h_all"), default=-1)
    pay_gmv = _num(_find_metric(report, "total_pay_order_gmv_for_roi2"), default=-1)
    if refund_gmv >= 0 and pay_gmv > 0:
        return round(min(1.0, refund_gmv / pay_gmv), 6)
    return None


async def _account_metrics(
    client: OceanEngineClient,
    advertiser_id: int,
    start_date: str,
    end_date: str,
) -> dict[str, Any]:
    start_time = f"{start_date} 00:00:00"
    end_time = f"{end_date} 23:59:59"
    cost_mf = pay_mf = settle_mf = 0.0
    roi_pay_w = 0.0
    roi_settle_w = 0.0
    refund_w = 0.0
    refund_gmv_mf = 0.0
    rate_weight = 0.0
    weight = 0.0
    plan_count = 0
    commission_base = 0.0
    page = 1
    try:
        while True:
            data = await client.list_uni_promotion(
                advertiser_id,
                start_time,
                end_time,
                marketing_goal="VIDEO_PROM_GOODS",
                fields=FIELDS,
                page=page,
                page_size=50,
            )
            for item in data.get("ad_list") or []:
                s = item.get("stats_info") or {}
                c = _num(s.get("stat_cost"))
                pay_raw = _num(s.get("total_pay_order_gmv_for_roi2"))
                settle_raw = _num(s.get("total_order_settle_amount_for_roi2_1h"))
                cost_mf += c
                pay_mf += pay_raw
                settle_mf += settle_raw
                plan_count += 1
                cost_yuan = c / 100000.0
                pay_yuan = pay_raw / 100000.0
                settle_yuan = settle_raw / 100000.0
                raw_roi = s.get("total_prepay_and_pay_order_roi2")
                roi = _num(raw_roi) if raw_roi not in (None, "") else 0.0
                gmv = cost_yuan * roi if roi > 0 and cost_yuan > 0 else pay_yuan
                if gmv <= 0:
                    gmv = pay_yuan
                # 退货率用金额反推。接口退款率偶发为 1，会把佣金乘成 0，利润变成负消耗。
                if gmv > 0:
                    rate = (gmv - settle_yuan) / gmv
                elif pay_yuan > 0:
                    rate = (pay_yuan - settle_yuan) / pay_yuan
                else:
                    rate = 0.0
                rate = min(1.0, max(0.0, rate))
                commission_base += gmv * (1 - rate)
                refund_gmv_mf += _num(s.get("total_refund_order_gmv_for_roi2_1h_all"))
                api_rate = _norm_rate(s.get("total_refund_order_gmv_for_roi2_1h_rate"))
                if pay_raw > 0 and api_rate is not None:
                    refund_w += api_rate * pay_raw
                    rate_weight += pay_raw
                if c > 0:
                    weight += c
                    roi_pay_w += _num(s.get("total_prepay_and_pay_order_roi2")) * c
                    roi_settle_w += _num(s.get("total_prepay_and_pay_settle_roi2_1h")) * c
            page_info = data.get("page_info") or {}
            total_page = int(page_info.get("total_page") or page)
            if page >= total_page or not (data.get("ad_list") or []):
                break
            page += 1
    except OceanEngineApiError as exc:
        logger.info("消耗拉取失败 advertiser_id=%s: %s", advertiser_id, exc)

    cost = _mf_to_yuan(cost_mf)
    pay_amount = _mf_to_yuan(pay_mf)
    settle_amount = _mf_to_yuan(settle_mf)
    pay_roi = (
        round(roi_pay_w / weight, 4)
        if weight > 0
        else (round(pay_amount / cost, 4) if cost else None)
    )
    settle_roi = round(roi_settle_w / weight, 4) if weight > 0 else None
    if pay_mf > 0 and refund_gmv_mf > 0:
        refund_rate = round(refund_gmv_mf / pay_mf, 6)
    elif rate_weight > 0:
        refund_rate = round(refund_w / rate_weight, 6)
    else:
        refund_rate = await _account_refund_rate(
            client, advertiser_id, start_date, end_date
        )
    return {
        "stat_cost": cost,
        "pay_amount": pay_amount,
        "pay_roi": pay_roi,
        "settle_amount": settle_amount,
        "settle_roi": settle_roi,
        "refund_rate_1h": refund_rate,
        "plan_count": plan_count,
        "commission_base": round(commission_base, 2),
    }


async def build_dashboard(
    db: Session,
    *,
    start_date: str | None = None,
    end_date: str | None = None,
    progress_cb: ProgressCb = None,
) -> dict[str, Any]:
    today = date.today().isoformat()
    start = start_date or today
    end = end_date or today
    settings = get_settings()
    concurrency = max(1, int(settings.DASHBOARD_CONCURRENCY or 8))

    rows = list(
        db.scalars(
            select(QianchuanAdvertiser).order_by(QianchuanAdvertiser.advertiser_id.asc())
        ).all()
    )
    await token_store.ensure_access_token(db)
    client = await token_store.build_client(db)

    total = len(rows)
    if progress_cb:
        await progress_cb(0, total, "准备拉取…")

    sem = asyncio.Semaphore(concurrency)
    metrics_map: dict[int, dict[str, Any]] = {}
    done = 0
    lock = asyncio.Lock()

    async def one(adv_id: int, name: str) -> None:
        nonlocal done
        async with sem:
            metrics_map[adv_id] = await _account_metrics(client, adv_id, start, end)
            async with lock:
                done += 1
                if progress_cb:
                    await progress_cb(done, total, f"已完成 {done}/{total} · {name or adv_id}")

    await asyncio.gather(
        *(one(int(r.advertiser_id), r.advertiser_name or "") for r in rows)
    )

    accounts: list[dict[str, Any]] = []
    sum_cost = sum_pay = sum_settle = 0.0
    for row in rows:
        adv_id = int(row.advertiser_id)
        m = metrics_map.get(adv_id) or {}
        cost = float(m.get("stat_cost") or 0)
        pay_amount = float(m.get("pay_amount") or 0)
        settle_amount = float(m.get("settle_amount") or 0)
        sum_cost += cost
        sum_pay += pay_amount
        sum_settle += settle_amount
        accounts.append(
            {
                "advertiser_id": adv_id,
                "advertiser_name": row.advertiser_name or "",
                "status": row.status or "-",
                "stat_cost": cost,
                "pay_amount": pay_amount,
                "pay_roi": m.get("pay_roi"),
                "settle_amount": settle_amount,
                "settle_roi": m.get("settle_roi"),
                "refund_rate_1h": m.get("refund_rate_1h"),
                "plan_count": int(m.get("plan_count") or 0),
                "commission_base": float(m.get("commission_base") or 0),
            }
        )

    accounts.sort(key=lambda x: float(x.get("stat_cost") or 0), reverse=True)

    return {
        "start_date": start,
        "end_date": end,
        "overview": {
            "stat_cost": round(sum_cost, 2),
            "pay_amount": round(sum_pay, 2),
            "pay_roi": round(sum_pay / sum_cost, 4) if sum_cost > 0 else None,
            "settle_amount": round(sum_settle, 2),
            "settle_roi": round(sum_settle / sum_cost, 4) if sum_cost > 0 else None,
        },
        "accounts": accounts,
        "total": len(accounts),
    }
