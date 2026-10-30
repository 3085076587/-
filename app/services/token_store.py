"""oauth_token 表读写与自动刷新。"""

from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.clients.oceanengine import OceanEngineClient
from app.core.exceptions import AppError
from app.models.oauth_token import OAuthToken


def _now() -> datetime:
    return datetime.now().replace(microsecond=0)


def get_token_row(db: Session) -> OAuthToken | None:
    return db.scalar(select(OAuthToken).order_by(OAuthToken.id.desc()).limit(1))


def require_token_row(db: Session) -> OAuthToken:
    row = get_token_row(db)
    if row is None:
        raise AppError("尚未保存 token，请先调用 /api/oauth/save-token 或完成授权回调", code=40110)
    return row


def save_token(
    db: Session,
    *,
    access_token: str,
    refresh_token: str,
    expires_in: int,
    refresh_token_expires_in: int,
    agent_id: int | None = None,
) -> OAuthToken:
    now = _now()
    row = get_token_row(db)
    if row is None:
        row = OAuthToken(
            access_token=access_token,
            refresh_token=refresh_token,
            expires_at=now + timedelta(seconds=int(expires_in)),
            refresh_expires_at=now + timedelta(seconds=int(refresh_token_expires_in)),
            agent_id=agent_id,
        )
        db.add(row)
    else:
        row.access_token = access_token
        row.refresh_token = refresh_token
        row.expires_at = now + timedelta(seconds=int(expires_in))
        row.refresh_expires_at = now + timedelta(seconds=int(refresh_token_expires_in))
        if agent_id is not None:
            row.agent_id = agent_id
    db.commit()
    db.refresh(row)
    return row


async def refresh_access_token(db: Session, row: OAuthToken | None = None) -> OAuthToken:
    row = row or require_token_row(db)
    if row.refresh_expires_at <= _now():
        raise AppError("refresh_token 已过期，请重新授权", code=40112)

    client = OceanEngineClient(access_token="")
    data = await client.refresh_token(row.refresh_token)
    row = save_token(
        db,
        access_token=data["access_token"],
        refresh_token=data.get("refresh_token") or row.refresh_token,
        expires_in=int(data.get("expires_in") or 86400),
        refresh_token_expires_in=int(data.get("refresh_token_expires_in") or 2592000),
        agent_id=row.agent_id,
    )
    return row


async def ensure_access_token(db: Session) -> OAuthToken:
    """若 access_token 临近过期则提前刷新。"""
    row = require_token_row(db)
    # 提前 5 分钟刷新，避免请求中途过期。
    if row.expires_at <= _now() + timedelta(minutes=5):
        row = await refresh_access_token(db, row)
    return row


async def build_client(db: Session) -> OceanEngineClient:
    row = await ensure_access_token(db)

    async def _refresher() -> str:
        refreshed = await refresh_access_token(db)
        return refreshed.access_token

    return OceanEngineClient(row.access_token, token_refresher=_refresher)
