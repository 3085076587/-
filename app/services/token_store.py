"""oauth_token 表读写与自动刷新。一个授权账号一行。"""

from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.clients.oceanengine import OceanEngineClient
from app.core.exceptions import AppError
from app.models.oauth_token import OAuthToken
from app.services import account_scope


def _now() -> datetime:
    return datetime.now().replace(microsecond=0)


def list_token_rows(db: Session) -> list[OAuthToken]:
    return list(db.scalars(select(OAuthToken).order_by(OAuthToken.id.asc())).all())


def selected_row(db: Session) -> OAuthToken | None:
    row = db.scalar(
        select(OAuthToken).where(OAuthToken.selected == 1).order_by(OAuthToken.id.desc()).limit(1)
    )
    if row is not None:
        return row
    return db.scalar(select(OAuthToken).order_by(OAuthToken.id.desc()).limit(1))


def get_token_row(db: Session, agent_id: int | None = None) -> OAuthToken | None:
    scoped = agent_id if agent_id is not None else account_scope.agent_id()
    if scoped:
        row = db.scalar(select(OAuthToken).where(OAuthToken.agent_id == int(scoped)))
        if row is not None:
            return row
    return selected_row(db)


def require_token_row(db: Session, agent_id: int | None = None) -> OAuthToken:
    row = get_token_row(db, agent_id)
    if row is None:
        raise AppError("尚未保存 token，请先调用 /api/oauth/save-token 或完成授权回调", code=40110)
    return row


def select_account(db: Session, agent_id: int) -> OAuthToken:
    found: OAuthToken | None = None
    for row in list_token_rows(db):
        chosen = int(row.agent_id or 0) == int(agent_id)
        row.selected = 1 if chosen else 0
        if chosen:
            found = row
    if found is None:
        raise AppError("没有这个授权账号", code=40413)
    db.commit()
    db.refresh(found)
    return found


def _apply_token_fields(
    row: OAuthToken,
    *,
    access_token: str,
    refresh_token: str,
    expires_in: int,
    refresh_token_expires_in: int,
    agent_id: int | None,
    agent_name: str | None = None,
    account_role: str | None = None,
    account_kind: str | None = None,
) -> None:
    now = _now()
    row.access_token = access_token
    row.refresh_token = refresh_token
    row.expires_at = now + timedelta(seconds=int(expires_in))
    row.refresh_expires_at = now + timedelta(seconds=int(refresh_token_expires_in))
    if agent_id is not None:
        row.agent_id = int(agent_id)
    if agent_name is not None:
        row.agent_name = agent_name
    if account_role is not None:
        row.account_role = account_role
    if account_kind is not None:
        row.account_kind = account_kind


def save_token(
    db: Session,
    *,
    access_token: str,
    refresh_token: str,
    expires_in: int,
    refresh_token_expires_in: int,
    agent_id: int | None = None,
    agent_name: str | None = None,
    account_role: str | None = None,
    account_kind: str | None = None,
) -> OAuthToken:
    """按 agent_id 更新。没带 agent_id 时只更新当前选中的那条，不新增，避免把别的账号盖掉。"""
    row: OAuthToken | None = None
    if agent_id is not None:
        row = db.scalar(select(OAuthToken).where(OAuthToken.agent_id == int(agent_id)))
    else:
        row = selected_row(db)
    if row is None:
        row = OAuthToken(
            access_token=access_token,
            refresh_token=refresh_token,
            expires_at=_now(),
            refresh_expires_at=_now(),
            agent_id=agent_id,
            account_kind=account_kind or "org",
            selected=0 if list_token_rows(db) else 1,
        )
        db.add(row)
    _apply_token_fields(
        row,
        access_token=access_token,
        refresh_token=refresh_token,
        expires_in=expires_in,
        refresh_token_expires_in=refresh_token_expires_in,
        agent_id=agent_id,
        agent_name=agent_name,
        account_role=account_role,
        account_kind=account_kind,
    )
    if not any(int(item.selected or 0) == 1 for item in list_token_rows(db)):
        row.selected = 1
    db.commit()
    db.refresh(row)
    return row


async def refresh_access_token(db: Session, row: OAuthToken | None = None) -> OAuthToken:
    row = row or require_token_row(db)
    if row.refresh_expires_at <= _now():
        raise AppError("refresh_token 已过期，请重新授权", code=40112)

    client = OceanEngineClient(access_token="")
    old_refresh = row.refresh_token
    data = await client.refresh_token(old_refresh)
    new_access = data["access_token"]
    new_refresh = data.get("refresh_token") or old_refresh
    expires_in = int(data.get("expires_in") or 86400)
    refresh_expires_in = int(data.get("refresh_token_expires_in") or 2592000)
    siblings = list(
        db.scalars(select(OAuthToken).where(OAuthToken.refresh_token == old_refresh)).all()
    )
    if row not in siblings:
        siblings.append(row)
    for item in siblings:
        _apply_token_fields(
            item,
            access_token=new_access,
            refresh_token=new_refresh,
            expires_in=expires_in,
            refresh_token_expires_in=refresh_expires_in,
            agent_id=item.agent_id,
        )
    db.commit()
    db.refresh(row)
    return row


async def ensure_access_token(db: Session, agent_id: int | None = None) -> OAuthToken:
    """若 access_token 临近过期则提前刷新。同一 refresh_token 的账号会一起更新。"""
    row = require_token_row(db, agent_id)
    if row.expires_at <= _now() + timedelta(minutes=5):
        row = await refresh_access_token(db, row)
    return row


async def build_client(db: Session, agent_id: int | None = None) -> OceanEngineClient:
    row = await ensure_access_token(db, agent_id)

    async def _refresher() -> str:
        refreshed = await refresh_access_token(db, row)
        return refreshed.access_token

    return OceanEngineClient(row.access_token, token_refresher=_refresher)
