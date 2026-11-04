"""全域计划：实时列表、启停、改 ROI。"""

from __future__ import annotations

import asyncio
import re
from datetime import date, datetime, timedelta, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.clients.oceanengine import OceanEngineClient
from app.core.exceptions import AppError, OceanEngineApiError
from app.models.plan_commission import PlanCommission
from app.schemas.plan import UpdateRoiIn, UpdateRoiOut, UpdateStatusIn
from app.services import plan_commission_sync, reminder_service, token_store

PLAN_FIELDS = [
    "stat_cost",
    "total_pay_order_gmv_for_roi2",
    "total_prepay_and_pay_order_roi2",
    "total_order_settle_amount_for_roi2_1h",
    "total_prepay_and_pay_settle_roi2_1h",
    "total_refund_order_gmv_for_roi2_1h_rate",
    "total_refund_order_gmv_for_roi2_1h_all",
]

# 未配置佣金时默认按 60% 计利润；用户在页面上改了则存 plan_commission。
DEFAULT_COMMISSION_PCT = 60.0


def _num(value: Any, default: float = 0.0) -> float:
    try:
        return float(value) if value not in (None, "") else default
    except (TypeError, ValueError):
        return default


def _mf_to_yuan(value: Any) -> float:
    return round(_num(value) / 100000.0, 2)


def _norm_rate(value: Any) -> float | None:
    rate = _num(value, default=-1)
    if rate < 0:
        return None
    while rate > 1 and rate <= 10000:
        rate = rate / 100.0
    if rate > 1:
        return None
    return round(rate, 6)


_STATUS_LABELS = {
    "DELIVERY_OK": "投放中",
    "ENABLE": "投放中",
    "DELIVERING": "投放中",
    "DISABLE": "已暂停",
    "DISABLED": "已暂停",
    "PAUSED": "已暂停",
    "SYSTEM_DISABLE": "系统暂停",
    "OFFLINE_BUDGET": "预算不足",
    "OFFLINE_BALANCE": "余额不足",
    "AUDIT": "审核中",
    "REAUDIT": "审核中",
    "AUDIT_DENY": "审核不通过",
    "TIME_NO_REACH": "未到投放时间",
    "NO_SCHEDULE": "不在投放时段",
    "FROZEN": "已终止",
    "DELETE": "已删除",
    "DELETED": "已删除",
}


def _status_label(ad_info: dict[str, Any]) -> str:
    status = str(ad_info.get("status") or "")
    opt = str(ad_info.get("opt_status") or "")
    if status in _STATUS_LABELS:
        return _STATUS_LABELS[status]
    if opt == "DISABLE":
        return "已暂停"
    if opt == "ENABLE":
        return "投放中"
    return status or opt or "-"


async def list_uni_plans(
    db: Session,
    advertiser_id: int,
    *,
    start_date: str | None = None,
    end_date: str | None = None,
    sync_commission: bool = True,
) -> dict[str, Any]:
    today = date.today().isoformat()
    start = start_date or today
    end = end_date or today
    start_time = f"{start} 00:00:00"
    end_time = f"{end} 23:59:59"
    client = await token_store.build_client(db)
    saved_commission = {
        int(row.ad_id): float(row.commission_pct)
        for row in db.scalars(
            select(PlanCommission).where(PlanCommission.advertiser_id == advertiser_id)
        ).all()
        if float(row.commission_pct) > 0
    }

    plans: list[dict[str, Any]] = []
    page = 1
    expected = 0
    while True:
        data = None
        last_exc: OceanEngineApiError | None = None
        for attempt in range(3):
            try:
                data = await client.list_uni_promotion(
                    advertiser_id,
                    start_time,
                    end_time,
                    marketing_goal="VIDEO_PROM_GOODS",
                    fields=PLAN_FIELDS,
                    page=page,
                    page_size=50,
                )
                break
            except OceanEngineApiError as exc:
                last_exc = exc
                if "超时" not in str(exc) or attempt == 2:
                    raise
                await asyncio.sleep(1.2 * (attempt + 1))
        if data is None:
            raise last_exc or AppError("拉取计划失败")
        for item in data.get("ad_list") or []:
            ad_info = item.get("ad_info") or {}
            stats = item.get("stats_info") or {}
            products = item.get("product_info") or []
            product_name = ""
            product_image = ""
            product_id = None
            if products and isinstance(products[0], dict):
                product_name = str(products[0].get("product_name") or "")
                product_image = str(products[0].get("product_image") or "")
                raw_pid = products[0].get("product_id")
                try:
                    product_id = int(raw_pid) if raw_pid not in (None, "") else None
                except (TypeError, ValueError):
                    product_id = None
            ad_id = int(ad_info.get("id") or 0)
            if not ad_id:
                continue
            ad_name = str(ad_info.get("name") or "")
            cost = _mf_to_yuan(stats.get("stat_cost"))
            settle = _mf_to_yuan(stats.get("total_order_settle_amount_for_roi2_1h"))
            pay = _mf_to_yuan(stats.get("total_pay_order_gmv_for_roi2"))
            pay_roi = _num(stats.get("total_prepay_and_pay_order_roi2")) or None
            pay_raw = _num(stats.get("total_pay_order_gmv_for_roi2"))
            refund_gmv = _num(stats.get("total_refund_order_gmv_for_roi2_1h_all"))
            api_rate = _norm_rate(stats.get("total_refund_order_gmv_for_roi2_1h_rate"))
            if pay_raw > 0 and refund_gmv > 0:
                refund_rate = round(min(1.0, refund_gmv / pay_raw), 6)
            else:
                refund_rate = api_rate
            plans.append(
                {
                    "advertiser_id": advertiser_id,
                    "ad_id": ad_id,
                    "ad_name": ad_name,
                    "product_id": product_id,
                    "product_name": product_name,
                    "product_image": product_image,
                    "opt_status": str(ad_info.get("opt_status") or ""),
                    "status": str(ad_info.get("status") or ""),
                    "status_label": _status_label(ad_info),
                    "enabled": str(ad_info.get("opt_status") or "") != "DISABLE",
                    "budget": ad_info.get("budget"),
                    "budget_mode": ad_info.get("budget_mode"),
                    "roi2_goal": ad_info.get("roi2_goal"),
                    "deep_external_action": ad_info.get("deep_external_action"),
                    "stat_cost": cost,
                    "pay_amount": pay,
                    "pay_roi": pay_roi,
                    "settle_amount": settle,
                    "settle_roi": _num(stats.get("total_prepay_and_pay_settle_roi2_1h")) or None,
                    "refund_rate_1h": refund_rate,
                    "commission_pct": plan_commission_sync.resolve_plan_commission_pct(
                        ad_name, ad_id, saved_commission
                    ),
                    "commission_format_warning": plan_commission_sync.commission_name_format_warning(
                        ad_name
                    ),
                }
            )
        page_info = data.get("page_info") or {}
        batch = data.get("ad_list") or []
        total_number = int(page_info.get("total_number") or page_info.get("total_num") or 0)
        if total_number > expected:
            expected = total_number
        total_page = int(page_info.get("total_page") or 0)
        if total_number > 0:
            has_more = page * 50 < total_number
        elif total_page > 0:
            has_more = page < total_page
        else:
            has_more = len(batch) >= 50
        if not batch or not has_more:
            break
        page += 1

    if expected and len(plans) < expected:
        raise AppError(f"账户 {advertiser_id} 的计划没有拉全（{len(plans)}/{expected}）")

    if sync_commission and plans:
        await plan_commission_sync.sync_advertiser_plan_commissions(
            db,
            advertiser_id,
            start=start,
            end=end,
            plans=plans,
        )
        saved_commission = {
            int(row.ad_id): float(row.commission_pct)
            for row in db.scalars(
                select(PlanCommission).where(PlanCommission.advertiser_id == advertiser_id)
            ).all()
            if float(row.commission_pct) > 0
        }
        for item in plans:
            ad_id = int(item.get("ad_id") or 0)
            item["commission_pct"] = plan_commission_sync.resolve_plan_commission_pct(
                str(item.get("ad_name") or ""),
                ad_id,
                saved_commission,
            )

    return {
        "advertiser_id": advertiser_id,
        "start_date": start,
        "end_date": end,
        "plans": plans,
        "total": len(plans),
    }


def save_plan_commission(
    db: Session,
    advertiser_id: int,
    ad_id: int,
    commission_pct: float | None,
) -> dict[str, Any]:
    """佣金为空或 0 时删掉记录，页面上不显示利润。

    计划名里带 (佣金:xx%) 时，以名称解析为准；定时同步会覆盖手填值。
    名称里没有佣金标记时，手填值保留。
    """
    row = db.scalar(
        select(PlanCommission).where(
            PlanCommission.advertiser_id == advertiser_id,
            PlanCommission.ad_id == ad_id,
        )
    )
    pct = float(commission_pct or 0)
    if pct <= 0:
        if row is not None:
            db.delete(row)
            db.commit()
        return {"advertiser_id": advertiser_id, "ad_id": ad_id, "commission_pct": None}
    if row is None:
        row = PlanCommission(advertiser_id=advertiser_id, ad_id=ad_id, commission_pct=pct)
        db.add(row)
    else:
        row.commission_pct = pct
    db.commit()
    return {"advertiser_id": advertiser_id, "ad_id": ad_id, "commission_pct": pct}


def _already_deleted(plan: Any) -> bool:
    label = str(getattr(plan, "status_label", "") or "")
    blob = " ".join(
        [
            str(getattr(plan, "status", "") or ""),
            str(getattr(plan, "opt_status", "") or ""),
            label,
        ]
    ).upper()
    return "DELETE" in blob or "已删除" in label


def _status_failures(data: Any) -> list[str]:
    if not isinstance(data, dict):
        return []
    messages: list[str] = []
    rows = data.get("errors") or data.get("error_list") or data.get("fail_list") or []
    if isinstance(rows, list):
        for row in rows:
            if isinstance(row, dict):
                msg = row.get("error_message") or row.get("message") or row.get("error_msg")
                ad_id = row.get("ad_id")
                if msg:
                    messages.append(f"计划 {ad_id}：{msg}" if ad_id else str(msg))
            elif row:
                messages.append(str(row))
    err = data.get("error")
    if isinstance(err, str) and err.strip():
        messages.append(err.strip())
    return messages


async def delete_plans(db: Session, payload: Any) -> dict[str, Any]:
    """删除推商品计划。已删除的不再请求；有消耗的仍提交，平台拒绝时把原因返回。"""
    plans = list(payload.plans or [])
    if len(plans) > 10:
        raise AppError("一次最多删除 10 个计划")
    deleted_names = [str(plan.ad_id) for plan in plans if _already_deleted(plan)]
    if deleted_names:
        raise AppError("这些计划已经删除：" + "、".join(deleted_names))
    ad_ids: list[int] = []
    spent: list[str] = []
    for plan in plans:
        try:
            ad_id = int(str(plan.ad_id).strip())
        except (TypeError, ValueError):
            continue
        if ad_id and ad_id not in ad_ids:
            ad_ids.append(ad_id)
        cost = plan.stat_cost
        if cost is not None and float(cost) > 0:
            spent.append(str(ad_id))
    if not ad_ids:
        raise AppError("请选择要删除的计划")
    client = await token_store.build_client(db)
    try:
        data = await client.update_uni_promotion_status(int(payload.advertiser_id), ad_ids, "DELETE")
    except OceanEngineApiError as exc:
        raise AppError(exc.msg, data=exc.data) from exc
    failures = _status_failures(data)
    if failures:
        raise AppError("；".join(failures), data=data if isinstance(data, dict) else None)
    return {
        "advertiser_id": int(payload.advertiser_id),
        "ad_ids": ad_ids,
        "opt_status": "DELETE",
        "spent_ids": spent,
        "oceanengine": data if isinstance(data, dict) else {"raw": data},
    }


async def update_plan_status(db: Session, payload: UpdateStatusIn) -> dict[str, Any]:
    if payload.opt_status not in {"ENABLE", "DISABLE"}:
        raise AppError("opt_status 只允许 ENABLE / DISABLE")
    client = await token_store.build_client(db)
    data = await client.update_uni_promotion_status(
        payload.advertiser_id,
        [payload.ad_id],
        payload.opt_status,
    )
    return {
        "advertiser_id": payload.advertiser_id,
        "ad_id": payload.ad_id,
        "opt_status": payload.opt_status,
        "oceanengine": data if isinstance(data, dict) else {"raw": data},
    }


async def update_roi_with_reminder(db: Session, payload: UpdateRoiIn) -> dict[str, Any]:
    client = await token_store.build_client(db)
    ocean_data = await client.update_roi2_goal(
        payload.advertiser_id,
        payload.ad_id,
        payload.roi2_goal,
        deep_external_action=payload.deep_external_action
        or "AD_CONVERT_TYPE_LIVE_PURE_PAY_ROI",
    )
    reminder = None
    if payload.remind_after_seconds and payload.remind_after_seconds > 0:
        reminder = await reminder_service.create_reminder(
            advertiser_id=payload.advertiser_id,
            ad_id=payload.ad_id,
            ad_name=payload.ad_name or "",
            roi2_goal=payload.roi2_goal,
            remind_after_seconds=payload.remind_after_seconds,
        )
    out = UpdateRoiOut(
        advertiser_id=payload.advertiser_id,
        ad_id=payload.ad_id,
        roi2_goal=payload.roi2_goal,
        oceanengine=ocean_data if isinstance(ocean_data, dict) else {"raw": ocean_data},
    ).model_dump()
    out["reminder"] = reminder
    return out


_SHANGHAI = timezone(timedelta(hours=8))
_FOCUS_WORDS = ("roi", "预算", "budget", "启停", "暂停", "启用", "开关", "opt_status", "enable", "disable", "投放状态")
_CHANGE_RE = re.compile(
    r"^(?P<field>.+?)[:：]\s*(?P<before>.+?)\s*(?:->|→|=>|改为|修改为)\s*(?P<after>.+)$"
)
_FROM_TO_RE = re.compile(r"从(?P<before>.+?)(?:修改|调整)?为(?P<after>.+)$")


def _is_focus(*parts: object) -> bool:
    text = " ".join(str(part or "") for part in parts).lower()
    return any(word.lower() in text for word in _FOCUS_WORDS)


def _change_from_text(text: str) -> dict[str, Any]:
    raw = text.strip()
    matched = _CHANGE_RE.match(raw) or _FROM_TO_RE.search(raw)
    if matched:
        field = str(matched.groupdict().get("field") or "").strip()
        before = str(matched.group("before") or "").strip()
        after = str(matched.group("after") or "").strip()
    else:
        field, before, after = "", "", raw
    return {
        "field": field,
        "before": before,
        "after": after,
        "raw": raw,
        "focus": _is_focus(field, before, after, raw),
    }


def _change_from_item(item: Any) -> dict[str, Any]:
    if isinstance(item, dict):
        field = str(item.get("field") or item.get("name") or item.get("key") or item.get("title") or "")
        before = item.get("old")
        if before is None:
            before = item.get("before") or item.get("old_value") or item.get("origin") or ""
        after = item.get("new")
        if after is None:
            after = item.get("after") or item.get("new_value") or item.get("update") or ""
        raw = str(item.get("content") or item.get("text") or "")
        if not raw:
            raw = f"{field}: {before} -> {after}".strip(": ")
        return {
            "field": field,
            "before": str(before),
            "after": str(after),
            "raw": raw,
            "focus": _is_focus(field, before, after, raw),
        }
    return _change_from_text(str(item or ""))


def _log_rows(entry: dict[str, Any]) -> dict[str, Any]:
    changes = [_change_from_item(item) for item in entry.get("content_log") or []]
    for sub in entry.get("sub_logs") or []:
        if not isinstance(sub, dict):
            continue
        title = str(sub.get("content_title") or "")
        for item in sub.get("content_log") or []:
            change = _change_from_item(item)
            if title and not change["field"]:
                change["field"] = title
                change["focus"] = _is_focus(title, change["before"], change["after"], change["raw"])
            changes.append(change)
    changes.sort(key=lambda row: 0 if row["focus"] else 1)
    return {
        "create_time": str(entry.get("create_time") or ""),
        "operator_name": str(entry.get("operator_name") or entry.get("operator") or entry.get("operator_id") or ""),
        "content_title": str(entry.get("content_title") or ""),
        "changes": changes,
    }


def _log_window(start_date: str, end_date: str) -> tuple[str, str]:
    today = datetime.now(_SHANGHAI).date()
    try:
        start = date.fromisoformat(start_date)
        end = date.fromisoformat(end_date)
    except ValueError as exc:
        raise AppError("日志时间格式要用 yyyy-mm-dd") from exc
    if end < start:
        raise AppError("结束日期不能早于开始日期")
    earliest = today - timedelta(days=180)
    if start < earliest:
        start = earliest
    if end > today:
        end = today
    now = datetime.now(_SHANGHAI)
    end_time = f"{end.isoformat()} 23:59:59"
    if end == today:
        end_time = now.strftime("%Y-%m-%d %H:%M:%S")
    return f"{start.isoformat()} 00:00:00", end_time


async def list_plan_logs(
    db: Session,
    advertiser_id: int,
    ad_id: int,
    start_date: str,
    end_date: str,
    page: int = 1,
) -> dict[str, Any]:
    start_time, end_time = _log_window(start_date, end_date)
    client = await token_store.build_client(db)
    try:
        data = await client.search_operation_logs(
            advertiser_id,
            ad_id,
            start_time,
            end_time,
            page=max(1, page),
            page_size=20,
        )
    except OceanEngineApiError as exc:
        message = exc.msg
        if any(word in message for word in ("权限", "permission", "scope", "无权限")):
            message = f"{message}。这个接口需要【工具-查询工具】权限，开通后要重新授权。"
        raise AppError(message, data=exc.data) from exc
    page_info = data.get("page_info") or {}
    logs = [_log_rows(entry) for entry in data.get("logs") or [] if isinstance(entry, dict)]
    return {
        "advertiser_id": advertiser_id,
        "ad_id": str(ad_id),
        "start_time": start_time,
        "end_time": end_time,
        "logs": logs,
        "page": int(page_info.get("page") or page or 1),
        "total_page": int(page_info.get("total_page") or 1),
        "total": int(page_info.get("total_number") or len(logs)),
    }
