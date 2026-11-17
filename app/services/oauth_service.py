"""OAuth：按授权账号分别保存 token。一次授权里出现的账号都留下，不覆盖其他账号。"""

from __future__ import annotations

import logging

from sqlalchemy.orm import Session

from app.clients.oceanengine import OceanEngineClient
from app.core.exceptions import AppError
from app.models.oauth_token import OAuthToken
from app.schemas.oauth import FetchAgentIdOut, SaveTokenIn, SaveTokenOut, TokenRefreshOut
from app.services import token_store

logger = logging.getLogger("uvicorn.error")

_ORG_ROLES = {
    "PLATFORM_ROLE_QIANCHUAN_AGENT",
    "AGENT",
    "CHILD_AGENT",
    "AGENT_SYSTEM_ACCOUNT",
    "CUSTOMER_ADMIN",
    "CUSTOMER_OPERATOR",
}


def account_kind(role: str) -> str:
    """升级版组织、代理商、旧版管理员下面还有子账户。其余按这一家店自己处理。"""
    text = role or ""
    if text.startswith("PLATFORM_ROLE_ENTERPRISE_BP") or text.endswith("ENTERPRISE_BP_ADMIN"):
        return "org"
    if text.endswith("ENTERPRISE_BP_OPERATOR"):
        return "org"
    if text in _ORG_ROLES:
        return "org"
    return "shop"


def _account_id(item: dict) -> int:
    return int(item.get("advertiser_id") or item.get("account_id") or 0)


def _account_name(item: dict) -> str:
    return str(item.get("advertiser_name") or item.get("account_name") or "")


async def save_token(db: Session, payload: SaveTokenIn) -> SaveTokenOut:
    rows = await bind_token_accounts(
        db,
        access_token=payload.access_token,
        refresh_token=payload.refresh_token,
        expires_in=payload.expires_in,
        refresh_token_expires_in=payload.refresh_token_expires_in,
    )
    row = token_store.selected_row(db) or rows[0]
    return SaveTokenOut(
        id=row.id,
        expires_at=row.expires_at,
        refresh_expires_at=row.refresh_expires_at,
        agent_id=row.agent_id,
    )


async def bind_token_accounts(
    db: Session,
    *,
    access_token: str,
    refresh_token: str,
    expires_in: int,
    refresh_token_expires_in: int,
) -> list[OAuthToken]:
    """用这次授权能看到的账号各写一行。库里已有的其他账号不动。"""
    client = OceanEngineClient(access_token)
    data = await client.get_authorized_advertisers()
    accounts = data.get("list") or []
    if not accounts:
        raise AppError("当前 token 下没有已授权账户", code=40410, data=data)

    saved: list[OAuthToken] = []
    for item in accounts:
        agent_id = _account_id(item)
        if not agent_id:
            continue
        role = str(item.get("account_role") or "")
        row = token_store.save_token(
            db,
            access_token=access_token,
            refresh_token=refresh_token,
            expires_in=expires_in,
            refresh_token_expires_in=refresh_token_expires_in,
            agent_id=agent_id,
            agent_name=_account_name(item),
            account_role=role,
            account_kind=account_kind(role),
        )
        saved.append(row)
        logger.info(
            "已保存授权账号 agent_id=%s name=%s role=%s kind=%s",
            row.agent_id,
            row.agent_name,
            row.account_role,
            row.account_kind,
        )
    if not saved:
        raise AppError("无法从授权账户中解析 agent_id", code=40411, data=accounts)
    selected = token_store.selected_row(db)
    if selected is None or int(selected.selected or 0) != 1:
        token_store.select_account(db, int(saved[0].agent_id or 0))
    return saved


async def fetch_agent_id(db: Session) -> FetchAgentIdOut:
    row = token_store.require_token_row(db)
    now = token_store._now()
    expires_in = max(60, int((row.expires_at - now).total_seconds()))
    refresh_expires_in = max(60, int((row.refresh_expires_at - now).total_seconds()))
    await bind_token_accounts(
        db,
        access_token=row.access_token,
        refresh_token=row.refresh_token,
        expires_in=expires_in,
        refresh_token_expires_in=refresh_expires_in,
    )
    current = token_store.get_token_row(db, row.agent_id) or token_store.require_token_row(db)
    return FetchAgentIdOut(
        agent_id=int(current.agent_id or 0),
        agent_name=current.agent_name,
        account_role=current.account_role,
    )


async def refresh_token(db: Session) -> TokenRefreshOut:
    row = await token_store.refresh_access_token(db)
    return TokenRefreshOut(
        expires_at=row.expires_at,
        refresh_expires_at=row.refresh_expires_at,
        agent_id=row.agent_id,
    )
