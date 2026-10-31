"""子账户页：从巨量工作台账户表读取商品投放数据。按英文括号里的人名分开。"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from datetime import date
from typing import Any, Awaitable, Callable

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.clients.workbench import fetch_product_accounts
from app.core.database import SessionLocal
from app.core.exceptions import AppError
from app.models.advertiser import QianchuanAdvertiser
from app.services import workbench_session

logger = logging.getLogger("uvicorn.error")


_PERSON_RE = re.compile(r"\(([^()]*)\)")
_load_lock = asyncio.Lock()
_load_tasks: dict[str, asyncio.Task] = {}
_load_cache: dict[str, tuple[float, list[dict[str, Any]]]] = {}
_LOAD_CACHE_SECONDS = 15


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


def people_from_accounts(accounts: list[dict[str, Any]]) -> list[str]:
    names: list[str] = []
    seen: set[str] = set()
    for item in accounts:
        name = person_name(str(item.get("advertiser_name") or ""))
        if not name or name in seen or name == "admin":
            continue
        seen.add(name)
        names.append(name)
    return names


ProgressCb = Callable[[int, int, str], Awaitable[None]] | None


async def _fetch_accounts(start: str, end: str) -> list[dict[str, Any]]:
    async with workbench_session.fetch_lock:
        return await _fetch_accounts_locked(start, end)


async def _fetch_accounts_locked(start: str, end: str) -> list[dict[str, Any]]:
    db = SessionLocal()
    row = None
    try:
        row = workbench_session.require_session(db)
        accounts, cookie, csrf = await fetch_product_accounts(
            ebpid=int(row.ebpid),
            cookie=row.cookie,
            csrf=row.csrf_token or "",
            start_date=start,
            end_date=end,
        )
        workbench_session.mark_ok(db, row, cookie, csrf)
        return accounts
    except Exception as exc:
        if row is not None and not isinstance(exc, AppError):
            workbench_session.mark_fail(db, row, str(exc))
        elif row is not None and isinstance(exc, AppError) and exc.code != 40421:
            workbench_session.mark_fail(db, row, exc.msg)
        raise
    finally:
        db.close()


async def load_roi2_accounts(start: str, end: str) -> list[dict[str, Any]]:
    """同一轮刷新里，多个人共用这一次工作台请求。"""
    key = f"{start}|{end}"
    now = time.monotonic()
    cached = _load_cache.get(key)
    if cached and now - cached[0] < _LOAD_CACHE_SECONDS:
        return list(cached[1])

    async with _load_lock:
        cached = _load_cache.get(key)
        if cached and time.monotonic() - cached[0] < _LOAD_CACHE_SECONDS:
            return list(cached[1])
        task = _load_tasks.get(key)
        if task is None or task.done():
            task = asyncio.create_task(_fetch_accounts(start, end))
            _load_tasks[key] = task

    try:
        rows = await task
    finally:
        async with _load_lock:
            if task.done() and not task.cancelled() and task.exception() is None:
                _load_cache[key] = (time.monotonic(), list(task.result()))
            if _load_tasks.get(key) is task and task.done():
                _load_tasks.pop(key, None)
    return list(rows)


def _overview(accounts: list[dict[str, Any]]) -> dict[str, Any]:
    sum_cost = sum(float(item.get("stat_cost") or 0) for item in accounts)
    sum_pay = sum(float(item.get("pay_amount") or 0) for item in accounts)
    sum_settle = sum(float(item.get("settle_amount") or 0) for item in accounts)
    return {
        "stat_cost": round(sum_cost, 2),
        "pay_amount": round(sum_pay, 2),
        "pay_roi": round(sum_pay / sum_cost, 4) if sum_cost > 0 else None,
        "settle_amount": round(sum_settle, 2),
        "settle_roi": round(sum_settle / sum_cost, 4) if sum_cost > 0 else None,
    }


def dashboard_from_accounts(
    accounts: list[dict[str, Any]],
    start: str,
    end: str,
    person: str | None,
) -> dict[str, Any]:
    chosen = []
    for item in accounts:
        name = str(item.get("advertiser_name") or "")
        if not _has_parentheses(name):
            continue
        if person is not None and person_name(name) != person:
            continue
        chosen.append(dict(item))
    chosen.sort(key=lambda item: float(item.get("stat_cost") or 0), reverse=True)
    return {
        "start_date": start,
        "end_date": end,
        "overview": _overview(chosen),
        "accounts": chosen,
        "total": len(chosen),
        "stale_count": 0,
    }


async def build_dashboard(
    db: Session,
    *,
    start_date: str | None = None,
    end_date: str | None = None,
    progress_cb: ProgressCb = None,
    previous_accounts: list[dict[str, Any]] | None = None,
    person: str | None = None,
    client: Any = None,
    gate: asyncio.Semaphore | None = None,
) -> dict[str, Any]:
    del db, previous_accounts, client, gate
    today = date.today().isoformat()
    start = start_date or today
    end = end_date or today
    total = 1
    if progress_cb:
        await progress_cb(0, total, "正在读取工作台账户表")
    accounts = await load_roi2_accounts(start, end)
    data = dashboard_from_accounts(accounts, start, end, person)
    if progress_cb:
        await progress_cb(total, total, f"已完成 {data['total']} 个账户")
    return data
