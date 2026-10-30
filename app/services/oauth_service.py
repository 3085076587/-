"""OAuth：保存 token、识别管理账户 agent_id。"""

from __future__ import annotations

import logging

from sqlalchemy.orm import Session

from app.core.exceptions import AppError
from app.schemas.oauth import FetchAgentIdOut, SaveTokenIn, SaveTokenOut, TokenRefreshOut
from app.services import token_store

logger = logging.getLogger("uvicorn.error")

# 角色优先级：升级版工作台 > 千川代理商 > 旧版工作台管理员 > 协作者。
ROLE_PRIORITY = [
    "PLATFORM_ROLE_ENTERPRISE_BP_ADMIN",
    "PLATFORM_ROLE_ENTERPRISE_BP_OPERATOR",
    "PLATFORM_ROLE_QIANCHUAN_AGENT",
    "AGENT",
    "CHILD_AGENT",
    "AGENT_SYSTEM_ACCOUNT",
    "CUSTOMER_ADMIN",
    "CUSTOMER_OPERATOR",
]


def _pick_manage_account(accounts: list[dict]) -> dict:
    ranked: list[tuple[int, dict]] = []
    for item in accounts:
        role = str(item.get("account_role") or "")
        try:
            rank = ROLE_PRIORITY.index(role)
        except ValueError:
            rank = 999
        ranked.append((rank, item))
    ranked.sort(key=lambda x: x[0])
    return ranked[0][1]


async def save_token(db: Session, payload: SaveTokenIn) -> SaveTokenOut:
    row = token_store.save_token(
        db,
        access_token=payload.access_token,
        refresh_token=payload.refresh_token,
        expires_in=payload.expires_in,
        refresh_token_expires_in=payload.refresh_token_expires_in,
    )
    return SaveTokenOut(
        id=row.id,
        expires_at=row.expires_at,
        refresh_expires_at=row.refresh_expires_at,
        agent_id=row.agent_id,
    )


async def fetch_agent_id(db: Session) -> FetchAgentIdOut:
    client = await token_store.build_client(db)
    data = await client.get_authorized_advertisers()
    accounts = data.get("list") or []
    if not accounts:
        raise AppError("当前 token 下没有已授权账户", code=40410, data=data)

    agent = _pick_manage_account(accounts)
    agent_id = int(agent.get("advertiser_id") or agent.get("account_id") or 0)
    if not agent_id:
        raise AppError("无法从授权账户中解析 agent_id", code=40411, data=agent)

    row = token_store.require_token_row(db)
    row.agent_id = agent_id
    row.agent_name = str(agent.get("advertiser_name") or agent.get("account_name") or "")
    row.account_role = str(agent.get("account_role") or "")
    db.commit()
    db.refresh(row)

    logger.info(
        "已绑定管理账户 agent_id=%s name=%s role=%s",
        row.agent_id,
        row.agent_name,
        row.account_role,
    )
    return FetchAgentIdOut(
        agent_id=agent_id,
        agent_name=row.agent_name,
        account_role=row.account_role,
    )


async def refresh_token(db: Session) -> TokenRefreshOut:
    row = await token_store.refresh_access_token(db)
    return TokenRefreshOut(
        expires_at=row.expires_at,
        refresh_expires_at=row.refresh_expires_at,
        agent_id=row.agent_id,
    )
