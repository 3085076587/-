"""从计划名称解析佣金，并与 Redis 快照对比后写入 plan_commission。"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from datetime import date, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.redis_client import get_cache_redis
from app.models.advertiser import QianchuanAdvertiser
from app.models.plan_commission import PlanCommission

logger = logging.getLogger("uvicorn.error")

_SNAPSHOT_PREFIX = "ad_helper:plan_commission_snapshot:v1"
_SNAPSHOT_TTL = 86400 * 2
_COMMISSION_STRICT = re.compile(r"佣金\s*[:：]\s*(?P<pct>[0-9]+(?:\.[0-9]+)?)\s*%")
_COMMISSION_LENIENT = re.compile(r"佣金\s*[:：]?\s*(?P<pct>[0-9]+(?:[.．][0-9]+)?)\s*%?")
_SYNC_LOOKBACK_DAYS = 30


def _parse_pct(raw: str) -> float | None:
    text = str(raw or "").replace("．", ".")
    try:
        pct = float(text)
    except ValueError:
        return None
    if pct <= 0 or pct > 100:
        return None
    return round(pct, 2)


def parse_commission_from_plan_name(ad_name: str) -> float | None:
    """计划名里的 (佣金:76.32%) 等形式。没有则返回 None。"""
    text = str(ad_name or "")
    if not text or "佣金" not in text:
        return None
    match = _COMMISSION_STRICT.search(text) or _COMMISSION_LENIENT.search(text)
    if not match:
        return None
    return _parse_pct(match.group("pct"))


def commission_name_format_warning(ad_name: str) -> str | None:
    """计划名里像写了佣金但格式不规范时，返回提示文案。"""
    text = str(ad_name or "")
    if "佣金" not in text:
        return None
    if _COMMISSION_STRICT.search(text):
        return None

    issues: list[str] = []
    if re.search(r"佣金\s*[0-9]", text) and not re.search(r"佣金\s*[:：]", text):
        issues.append("「佣金」后缺少冒号，应为 佣金:68.16%")
    if re.search(r"佣金\s*[:：]?\s*[0-9]", text) and "%" not in text:
        issues.append("可能缺少百分号 %")
    if "，" in text:
        issues.append("不要用中文逗号，小数点请用英文句点")
    if "．" in text:
        issues.append("小数点请用英文 . 不要用中文 ．")
    if ("(" in text or ")" in text) and ("（" in text or "）" in text):
        issues.append("括号中英文不要混用")
    if ("（" in text and ")" in text and "）" not in text) or (
        "(" in text and "）" in text and "（" not in text
    ):
        issues.append("括号未成对")
    if parse_commission_from_plan_name(text) is None:
        issues.append("未能识别佣金比例，参考格式：(佣金:68.16%)")
    elif not issues:
        issues.append("佣金格式不标准，建议写成 佣金:数字%")

    return "；".join(issues)


def resolve_plan_commission_pct(
    ad_name: str,
    ad_id: int,
    saved_commission: dict[int, float],
) -> float | None:
    """手填佣金优先，没有再解析名称里的佣金，都没有返回 None。"""
    stored = saved_commission.get(int(ad_id))
    if stored is not None and float(stored) > 0:
        return round(float(stored), 2)
    return parse_commission_from_plan_name(ad_name)


def user_saved_commissions(db: Session, advertiser_id: int) -> dict[int, float]:
    """只取用户手填的计划佣金。名称同步写入的行不参与优先。"""
    rows = db.scalars(
        select(PlanCommission).where(
            PlanCommission.advertiser_id == advertiser_id,
            PlanCommission.filled_by_user == 1,
        )
    ).all()
    return {
        int(row.ad_id): round(float(row.commission_pct), 2)
        for row in rows
        if float(row.commission_pct) > 0
    }


def user_commissions_by_advertiser(
    db: Session, advertiser_ids: list[int]
) -> dict[int, dict[int, float]]:
    if not advertiser_ids:
        return {}
    rows = db.scalars(
        select(PlanCommission).where(
            PlanCommission.advertiser_id.in_(list(advertiser_ids)),
            PlanCommission.filled_by_user == 1,
        )
    ).all()
    out: dict[int, dict[int, float]] = {}
    for row in rows:
        if float(row.commission_pct) <= 0:
            continue
        out.setdefault(int(row.advertiser_id), {})[int(row.ad_id)] = round(float(row.commission_pct), 2)
    return out


def _snapshot_key(advertiser_id: int, start: str, end: str) -> str:
    return f"{_SNAPSHOT_PREFIX}:{advertiser_id}:{start}:{end}"


async def _read_snapshot(advertiser_id: int, start: str, end: str) -> dict[str, Any]:
    try:
        raw = await get_cache_redis().get(_snapshot_key(advertiser_id, start, end))
        if not raw:
            return {}
        data = json.loads(raw)
        return data if isinstance(data, dict) else {}
    except Exception as exc:  # noqa: BLE001
        logger.warning("读计划佣金快照失败 adv=%s：%s", advertiser_id, exc)
        return {}


async def _write_snapshot(advertiser_id: int, start: str, end: str, payload: dict[str, Any]) -> None:
    try:
        await get_cache_redis().set(
            _snapshot_key(advertiser_id, start, end),
            json.dumps(payload, ensure_ascii=False),
            ex=_SNAPSHOT_TTL,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("写计划佣金快照失败 adv=%s：%s", advertiser_id, exc)


def _delete_commission(db: Session, advertiser_id: int, ad_id: int) -> bool:
    row = db.scalar(
        select(PlanCommission).where(
            PlanCommission.advertiser_id == advertiser_id,
            PlanCommission.ad_id == ad_id,
        )
    )
    if row is None or int(row.filled_by_user or 0) == 1:
        return False
    db.delete(row)
    return True


def _upsert_commission(db: Session, advertiser_id: int, ad_id: int, pct: float) -> bool:
    row = db.scalar(
        select(PlanCommission).where(
            PlanCommission.advertiser_id == advertiser_id,
            PlanCommission.ad_id == ad_id,
        )
    )
    pct = round(float(pct), 2)
    if row is not None and int(row.filled_by_user or 0) == 1:
        return False
    if row is None:
        db.add(
            PlanCommission(
                advertiser_id=advertiser_id,
                ad_id=ad_id,
                commission_pct=pct,
                filled_by_user=0,
            )
        )
        return True
    if float(row.commission_pct) == pct:
        return False
    row.commission_pct = pct
    row.filled_by_user = 0
    return True


def _apply_diff(
    db: Session,
    advertiser_id: int,
    current: dict[str, dict[str, Any]],
    previous: dict[str, dict[str, Any]],
) -> dict[str, int]:
    """名称里的佣金只补没有手填的行。用户填过的佣金不被名称同步覆盖或删掉。"""
    stats = {"updated": 0, "deleted": 0, "skipped": 0}
    for ad_key, info in current.items():
        ad_id = int(ad_key)
        ad_name = str(info.get("ad_name") or "")
        parsed = parse_commission_from_plan_name(ad_name)
        prev = previous.get(ad_key) or {}
        if parsed is None:
            stats["skipped"] += 1
            continue
        prev_pct = prev.get("commission_from_name")
        prev_name = prev.get("ad_name")
        if prev_pct == parsed and prev_name == ad_name:
            continue
        if _upsert_commission(db, advertiser_id, ad_id, parsed):
            stats["updated"] += 1

    removed = set(previous.keys()) - set(current.keys())
    for ad_key in removed:
        ad_id = int(ad_key)
        if _delete_commission(db, advertiser_id, ad_id):
            stats["deleted"] += 1
    if stats["updated"] or stats["deleted"]:
        db.commit()
    return stats


async def sync_advertiser_plan_commissions(
    db: Session,
    advertiser_id: int,
    *,
    start: str | None = None,
    end: str | None = None,
    plans: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    today = date.today()
    end_d = today
    start_d = today - timedelta(days=_SYNC_LOOKBACK_DAYS - 1)
    start_s = start or start_d.isoformat()
    end_s = end or end_d.isoformat()

    if plans is None:
        from app.services import uni_plan_service

        data = await uni_plan_service.list_uni_plans(
            db,
            advertiser_id,
            start_date=start_s,
            end_date=end_s,
            sync_commission=False,
        )
        rows = list(data.get("plans") or [])
    else:
        rows = plans
    current: dict[str, dict[str, Any]] = {}
    for row in rows:
        ad_id = int(row.get("ad_id") or 0)
        if not ad_id:
            continue
        ad_name = str(row.get("ad_name") or "")
        parsed = parse_commission_from_plan_name(ad_name)
        current[str(ad_id)] = {
            "ad_name": ad_name,
            "commission_from_name": parsed,
        }

    previous_plans = (await _read_snapshot(advertiser_id, start_s, end_s)).get("plans") or {}
    stats = _apply_diff(db, advertiser_id, current, previous_plans)
    await _write_snapshot(
        advertiser_id,
        start_s,
        end_s,
        {
            "start_date": start_s,
            "end_date": end_s,
            "plans": current,
            "synced_at": today.isoformat(),
        },
    )
    return {"advertiser_id": advertiser_id, "plan_count": len(current), **stats}


async def sync_all_advertisers(db: Session) -> dict[str, Any]:
    settings = get_settings()
    gate = asyncio.Semaphore(max(1, int(settings.DASHBOARD_CONCURRENCY or 2)))
    adv_ids = [int(x) for x in db.scalars(select(QianchuanAdvertiser.advertiser_id)).all()]
    totals = {"advertisers": len(adv_ids), "updated": 0, "deleted": 0, "failed": 0}

    async def one(adv_id: int) -> None:
        async with gate:
            try:
                result = await sync_advertiser_plan_commissions(db, adv_id)
                totals["updated"] += int(result.get("updated") or 0)
                totals["deleted"] += int(result.get("deleted") or 0)
            except Exception as exc:  # noqa: BLE001
                totals["failed"] += 1
                logger.warning("计划佣金同步失败 adv=%s：%s", adv_id, exc)

    await asyncio.gather(*(one(adv) for adv in adv_ids))
    return totals
