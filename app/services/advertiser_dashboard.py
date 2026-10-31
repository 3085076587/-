"""子账户页：并发拉取商品全域消耗。按英文括号里的人名分开。"""

from __future__ import annotations

import asyncio
import logging
import re
from datetime import date
from typing import Any, Awaitable, Callable

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.clients.oceanengine import RATE_LIMIT_CODES, OceanEngineClient
from app.core.config import get_settings
from app.core.exceptions import OceanEngineApiError
from app.models.advertiser import QianchuanAdvertiser
from app.services import token_store

logger = logging.getLogger("uvicorn.error")


_PERSON_RE = re.compile(r"\(([^()]*)\)")


def _has_parentheses(name: str) -> bool:
    """只认英文括号，例如 hs-奥诚-6号(张晨乐)。中文括号（）不算。"""
    text = name or ""
    return "(" in text and ")" in text


def person_name(advertiser_name: str) -> str | None:
    """英文括号里的人名。hs-奥诚-6号(张晨乐) -> 张晨乐。"""
    match = _PERSON_RE.search(advertiser_name or "")
    if not match:
        return None
    name = match.group(1).strip()
    return name or None


def list_people(db: Session) -> list[str]:
    rows = db.scalars(select(QianchuanAdvertiser.advertiser_name)).all()
    names: list[str] = []
    seen: set[str] = set()
    for raw in rows:
        name = person_name(raw or "")
        if not name or name in seen or name == "admin":
            continue
        seen.add(name)
        names.append(name)
    return names


def count_person_accounts(db: Session, person: str) -> int:
    total = 0
    rows = db.scalars(select(QianchuanAdvertiser.advertiser_name)).all()
    for raw in rows:
        if person_name(raw or "") == person:
            total += 1
    return total

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


def _has_more(page_info: dict, page: int, page_size: int, batch_len: int) -> bool:
    if batch_len <= 0:
        return False
    total_number = int(page_info.get("total_number") or page_info.get("total_num") or 0)
    if total_number > 0:
        return page * page_size < total_number
    total_page = int(page_info.get("total_page") or 0)
    if total_page > 0:
        return page < total_page
    return batch_len >= page_size


def _report_yuan(value: Any) -> float | None:
    """账户报表金额，接口单位是元。"""
    if value in (None, ""):
        return None
    amount = _num(value, default=-1)
    if amount < 0:
        return None
    return round(amount, 2)


def _report_rows(report: dict[str, Any]) -> list[dict[str, Any]]:
    for key in ("list", "rows", "data_list"):
        value = report.get(key)
        if isinstance(value, list) and value and all(isinstance(item, dict) for item in value):
            return value
    return [report]


async def _account_metrics_from_report(
    client: OceanEngineClient,
    advertiser_id: int,
    start_date: str,
    end_date: str,
    fields: list[str],
) -> dict[str, Any] | None:
    """和巨量账户页同一口径：商品全域账户报表，不靠计划列表加总。"""
    report = await client.get_uni_promotion_report(
        advertiser_id,
        start_date,
        end_date,
        fields=fields,
        marketing_goal="VIDEO_PROM_GOODS",
    )
    if not isinstance(report, dict) or not report:
        return None
    rows = _report_rows(report)
    costs = [_report_yuan(_find_metric(row, "stat_cost")) for row in rows]
    pays = [_report_yuan(_find_metric(row, "total_pay_order_gmv_for_roi2")) for row in rows]
    if all(item is None for item in costs) and all(item is None for item in pays):
        return None
    cost = round(sum(item or 0.0 for item in costs), 2)
    pay_amount = round(sum(item or 0.0 for item in pays), 2)
    settles = [_report_yuan(_find_metric(row, "total_order_settle_amount_for_roi2_1h")) for row in rows]
    settle_amount = round(sum(item or 0.0 for item in settles), 2) if any(item is not None for item in settles) else 0.0
    refunds = [_report_yuan(_find_metric(row, "total_refund_order_gmv_for_roi2_1h_all")) for row in rows]
    refund_gmv = round(sum(item or 0.0 for item in refunds), 2) if any(item is not None for item in refunds) else None
    if len(rows) == 1:
        roi_raw = _find_metric(rows[0], "total_prepay_and_pay_order_roi2")
        pay_roi = _num(roi_raw) if roi_raw not in (None, "") else None
        settle_roi_raw = _find_metric(rows[0], "total_prepay_and_pay_settle_roi2_1h")
        settle_roi = _num(settle_roi_raw) if settle_roi_raw not in (None, "") else None
        refund_rate = _norm_rate(_find_metric(rows[0], "total_refund_order_gmv_for_roi2_1h_rate"))
    else:
        pay_roi = round(pay_amount / cost, 4) if cost > 0 and pay_amount > 0 else None
        settle_roi = round(settle_amount / cost, 4) if cost > 0 and settle_amount > 0 else None
        refund_rate = None
    if (pay_roi is None or pay_roi <= 0) and cost > 0 and pay_amount > 0:
        pay_roi = round(pay_amount / cost, 4)
    if (settle_roi is None or settle_roi <= 0) and cost > 0 and settle_amount > 0:
        settle_roi = round(settle_amount / cost, 4)
    if pay_amount > 0 and refund_gmv and refund_gmv > 0:
        refund_rate = round(min(1.0, refund_gmv / pay_amount), 6)
    return {
        "stat_cost": cost,
        "pay_amount": pay_amount,
        "pay_roi": pay_roi,
        "settle_amount": settle_amount,
        "settle_roi": settle_roi,
        "refund_rate_1h": refund_rate,
        "plan_count": 0,
        "commission_base": 0.0,
    }


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
    page_size = 100
    try:
        report_metrics = None
        last_exc: OceanEngineApiError | None = None
        for attempt in range(3):
            try:
                report_metrics = await _account_metrics_from_report(
                    client, advertiser_id, start_date, end_date, FIELDS
                )
                last_exc = None
                break
            except OceanEngineApiError as exc:
                last_exc = exc
                if "超时" not in str(exc) or attempt == 2:
                    break
                await asyncio.sleep(1.2 * (attempt + 1))
        if report_metrics is not None:
            report_metrics["fetch_ok"] = True
            return report_metrics
        if last_exc is not None and (
            last_exc.ocean_code in RATE_LIMIT_CODES
            or any(word in str(last_exc) for word in ("频率", "频控", "限流"))
        ):
            logger.warning("账户报表被频控，跳过计划加总 advertiser_id=%s: %s", advertiser_id, last_exc)
            return {
                "stat_cost": 0.0,
                "pay_amount": 0.0,
                "pay_roi": None,
                "settle_amount": 0.0,
                "settle_roi": None,
                "refund_rate_1h": None,
                "plan_count": 0,
                "commission_base": 0.0,
                "fetch_ok": False,
            }
        if last_exc is not None:
            logger.info("账户报表不可用，改加总计划 advertiser_id=%s: %s", advertiser_id, last_exc)
        while True:
            data = await client.list_uni_promotion(
                advertiser_id,
                start_time,
                end_time,
                marketing_goal="VIDEO_PROM_GOODS",
                fields=FIELDS,
                page=page,
                page_size=page_size,
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
            batch = data.get("ad_list") or []
            if not _has_more(page_info, page, page_size, len(batch)):
                break
            page += 1
    except OceanEngineApiError as exc:
        logger.warning("消耗拉取失败，保留该账户上次数据 advertiser_id=%s: %s", advertiser_id, exc)
        return {
            "stat_cost": 0.0,
            "pay_amount": 0.0,
            "pay_roi": None,
            "settle_amount": 0.0,
            "settle_roi": None,
            "refund_rate_1h": None,
            "plan_count": 0,
            "commission_base": 0.0,
            "fetch_ok": False,
        }

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
        "fetch_ok": True,
    }


async def build_dashboard(
    db: Session,
    *,
    start_date: str | None = None,
    end_date: str | None = None,
    progress_cb: ProgressCb = None,
    previous_accounts: list[dict[str, Any]] | None = None,
    person: str | None = None,
    client: OceanEngineClient | None = None,
    gate: asyncio.Semaphore | None = None,
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
    if client is None:
        await token_store.ensure_access_token(db)
        client = await token_store.build_client(db)

    fetch_rows = []
    for row in rows:
        adv_name = row.advertiser_name or ""
        if not _has_parentheses(adv_name):
            continue
        if person is not None and person_name(adv_name) != person:
            continue
        fetch_rows.append(row)
    total = len(fetch_rows)
    if progress_cb:
        await progress_cb(0, total, "准备拉取…")

    sem = gate or asyncio.Semaphore(concurrency)
    metrics_map: dict[int, dict[str, Any]] = {}
    done = 0
    lock = asyncio.Lock()

    async def one(adv_id: int, name: str) -> None:
        nonlocal done
        async with sem:
            try:
                metrics_map[adv_id] = await _account_metrics(client, adv_id, start, end)
            except Exception as exc:
                logger.warning("账户拉取异常 advertiser_id=%s: %s", adv_id, exc)
                metrics_map[adv_id] = {"fetch_ok": False}
            async with lock:
                done += 1
                if progress_cb:
                    await progress_cb(done, total, f"已完成 {done}/{total} · {name or adv_id}")

    await asyncio.gather(
        *(one(int(r.advertiser_id), r.advertiser_name or "") for r in fetch_rows)
    )

    accounts: list[dict[str, Any]] = []
    sum_cost = sum_pay = sum_settle = 0.0
    stale_count = 0
    for row in fetch_rows:
        adv_id = int(row.advertiser_id)
        name = row.advertiser_name or ""
        m = metrics_map.get(adv_id) or {"fetch_ok": False}
        if m.get("fetch_ok") is False:
            raise OceanEngineApiError(f"{name}（{adv_id}）这次没有拉成功")
        cost = float(m.get("stat_cost") or 0)
        pay_amount = float(m.get("pay_amount") or 0)
        settle_amount = float(m.get("settle_amount") or 0)
        sum_cost += cost
        sum_pay += pay_amount
        sum_settle += settle_amount
        accounts.append(
            {
                "advertiser_id": adv_id,
                "advertiser_name": name,
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
        "stale_count": stale_count,
    }
