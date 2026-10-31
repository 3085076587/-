"""工作台登录凭证的读写和保活。"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.clients.workbench import fetch_product_accounts, parse_cookie
from app.core.exceptions import AppError
from app.models.workbench_session import WorkbenchSession

logger = logging.getLogger("uvicorn.error")

KEEP_HOURS = 12
# 工作台登录只有一份。同时刷新时排队用这一把锁，避免两个人各写一次把凭证盖掉。
fetch_lock = asyncio.Lock()


def get_session(db: Session) -> WorkbenchSession | None:
    return db.scalar(select(WorkbenchSession).order_by(WorkbenchSession.id.desc()).limit(1))


def require_session(db: Session) -> WorkbenchSession:
    row = get_session(db)
    if row is None or not (row.cookie or "").strip():
        raise AppError("工作台登录还没有保存，无法拉取子账户数据", code=40421)
    return row


def save_session(
    db: Session,
    *,
    ebpid: int,
    cookie: str,
    csrf_token: str = "",
    expires_at: datetime | None = None,
) -> WorkbenchSession:
    csrf = csrf_token or parse_cookie(cookie).get("csrftoken", "")
    row = get_session(db)
    when = expires_at or (datetime.now() + timedelta(hours=KEEP_HOURS))
    if row is None:
        row = WorkbenchSession(
            ebpid=int(ebpid),
            cookie=cookie,
            csrf_token=csrf,
            expires_at=when,
            last_error="",
        )
        db.add(row)
    else:
        row.ebpid = int(ebpid)
        row.cookie = cookie
        row.csrf_token = csrf
        row.expires_at = when
        row.last_error = ""
    db.commit()
    db.refresh(row)
    return row


def mark_ok(db: Session, row: WorkbenchSession, cookie: str, csrf: str) -> None:
    row.cookie = cookie or row.cookie
    if csrf:
        row.csrf_token = csrf
    now = datetime.now()
    row.last_check_at = now
    row.expires_at = now + timedelta(hours=KEEP_HOURS)
    row.last_error = ""
    db.commit()


def mark_fail(db: Session, row: WorkbenchSession, message: str) -> None:
    row.last_check_at = datetime.now()
    row.last_error = (message or "工作台登录保活失败")[:500]
    db.commit()


async def keepalive(db: Session) -> None:
    """用今天的账户表打一页，确认登录还在，并把新 Cookie 写回。"""
    async with fetch_lock:
        db.expire_all()
        row = get_session(db)
        if row is None or not (row.cookie or "").strip():
            logger.info("工作台保活：还没有登录凭证，跳过")
            return
        today = datetime.now().strftime("%Y-%m-%d")
        try:
            _accounts, cookie, csrf = await fetch_product_accounts(
                ebpid=int(row.ebpid),
                cookie=row.cookie,
                csrf=row.csrf_token or "",
                start_date=today,
                end_date=today,
                page_size=10,
                max_pages=1,
            )
        except Exception as exc:
            logger.exception("工作台保活失败：%s", exc)
            mark_fail(db, row, str(exc))
            return
        mark_ok(db, row, cookie, csrf)
        logger.info("工作台登录保活成功，有效期顺延到 %s", row.expires_at)
