"""OAuth token 保存、拉取 agent_id、授权回调。"""

from __future__ import annotations

import html
import logging

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.api.deps import get_db
from app.clients.oceanengine import OceanEngineClient
from app.core.config import get_settings
from app.core.exceptions import AppError, OceanEngineApiError
from app.core.response import ok
from app.models.advertiser import QianchuanAdvertiser
from app.models.workbench_session import WorkbenchSession
from app.schemas.oauth import SaveTokenIn
from app.services import oauth_service, token_store

logger = logging.getLogger("uvicorn.error")
router = APIRouter(tags=["OAuth"])


class SelectAccountIn(BaseModel):
    agent_id: int = Field(..., description="要切换到的授权账号")


def _account_payload(db: Session) -> dict:
    rows = token_store.list_token_rows(db)
    now_rows = []
    for row in rows:
        if not row.agent_id:
            continue
        shop_count = db.scalar(
            select(func.count())
            .select_from(QianchuanAdvertiser)
            .where(QianchuanAdvertiser.agent_id == int(row.agent_id))
        )
        bench = db.scalar(select(WorkbenchSession).where(WorkbenchSession.ebpid == int(row.agent_id)))
        now_rows.append(
            {
                "agent_id": int(row.agent_id),
                "agent_name": row.agent_name or "",
                "account_role": row.account_role or "",
                "account_kind": row.account_kind or "org",
                "selected": int(row.selected or 0) == 1,
                "access_expired": row.expires_at <= token_store._now(),
                "advertiser_count": int(shop_count or 0),
                "has_workbench": bench is not None and bool((bench.cookie or "").strip()),
            }
        )
    return {"accounts": now_rows}


@router.get("/oauth/accounts", summary="授权账号列表")
def list_accounts(db: Session = Depends(get_db)):
    return ok(_account_payload(db))


@router.post("/oauth/select", summary="切换当前授权账号")
def select_account(payload: SelectAccountIn, db: Session = Depends(get_db)):
    token_store.select_account(db, payload.agent_id)
    return ok(_account_payload(db))


@router.post("/oauth/save-token", summary="保存 OAuth token")
async def save_token(payload: SaveTokenIn, db: Session = Depends(get_db)):
    """保存这次授权能看到的全部账号。已经在库里的其他账号不会被盖掉。"""
    rows = await oauth_service.bind_token_accounts(
        db,
        access_token=payload.access_token,
        refresh_token=payload.refresh_token,
        expires_in=payload.expires_in,
        refresh_token_expires_in=payload.refresh_token_expires_in,
    )
    current = token_store.selected_row(db) or rows[0]
    return ok(
        {
            "id": current.id,
            "expires_at": current.expires_at,
            "refresh_expires_at": current.refresh_expires_at,
            "agent_id": current.agent_id,
            "agent_name": current.agent_name,
            "account_role": current.account_role,
            "accounts": [
                {"agent_id": row.agent_id, "agent_name": row.agent_name, "account_kind": row.account_kind}
                for row in rows
            ],
        }
    )


@router.get("/oauth/fetch-agent-id", summary="拉取并回写代理商 agent_id", include_in_schema=False)
async def fetch_agent_id(db: Session = Depends(get_db)):
    """保留接口供排查；正常流程在授权回调里自动完成。"""
    data = await oauth_service.fetch_agent_id(db)
    return ok(data.model_dump())


@router.get("/oauth/callback", summary="巨量 OAuth 授权回调")
async def oauth_callback(
    request: Request,
    db: Session = Depends(get_db),
    code: str | None = Query(None, description="兼容参数名 code"),
    auth_code: str | None = Query(None, description="巨量授权码 auth_code"),
    state: str | None = Query(None, description="透传 state"),
):
    """接收巨量回调：换 token 入库，并立刻拉 agent_id。"""
    logger.info("巨量回调 URL：%s", str(request.url))
    query = dict(request.query_params)
    logger.info("巨量回调参数：%s", query)

    authorization_code = (auth_code or code or "").strip()
    if not authorization_code:
        raise AppError("缺少授权码 auth_code/code", code=40010, data={"query": query})

    settings = get_settings()
    if not settings.ocean_ready:
        raise AppError("未配置 OCEANENGINE_APP_ID / OCEANENGINE_SECRET", code=50010)

    client = OceanEngineClient(access_token="")
    try:
        token = await client.exchange_access_token(authorization_code)
    except OceanEngineApiError as exc:
        raise AppError(exc.msg, code=exc.code, data=exc.data) from exc

    logger.info(
        "巨量返回 token：access_token=%s... refresh_token=%s... expires_in=%s",
        str(token.get("access_token", ""))[:12],
        str(token.get("refresh_token", ""))[:12],
        token.get("expires_in"),
    )

    rows = await oauth_service.bind_token_accounts(
        db,
        access_token=token["access_token"],
        refresh_token=token["refresh_token"],
        expires_in=int(token.get("expires_in") or 86400),
        refresh_token_expires_in=int(token.get("refresh_token_expires_in") or 2592000),
    )
    lines = "<br>".join(
        html.escape(f"{row.agent_name or '-'} / {row.agent_id} / {row.account_kind or 'org'}")
        for row in rows
    )
    current = token_store.selected_row(db) or rows[0]

    accept = (request.headers.get("accept") or "").lower()
    if "text/html" in accept:
        return HTMLResponse(
            f"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <title>授权成功</title>
  <meta http-equiv="refresh" content="2;url=/">
  <style>
    body {{ font-family: "Segoe UI","Microsoft YaHei",sans-serif; background:#f4f7f6; color:#1c1917; }}
    .box {{ max-width:520px; margin:12vh auto; background:#fff; border:1px solid #e7e5e4; border-radius:14px; padding:28px; }}
    h1 {{ color:#0f766e; font-size:22px; margin-bottom:12px; }}
    p {{ line-height:1.7; color:#57534e; }}
    a {{ color:#0f766e; }}
  </style>
</head>
<body>
  <div class="box">
    <h1>授权成功</h1>
    <p>这次授权里的账号已经分开入库。原来的其他账号还在。</p>
    <p>{lines}</p>
    <p>2 秒后返回首页，或 <a href="/">立即返回</a>。</p>
  </div>
</body>
</html>"""
        )

    return ok(
        {
            "state": state,
            "token_id": current.id,
            "expires_at": current.expires_at,
            "refresh_expires_at": current.refresh_expires_at,
            "agent_id": current.agent_id,
            "agent_name": current.agent_name,
            "account_role": current.account_role,
            "accounts": [
                {
                    "agent_id": row.agent_id,
                    "agent_name": row.agent_name,
                    "account_role": row.account_role,
                    "account_kind": row.account_kind,
                }
                for row in rows
            ],
            "advertiser_ids": token.get("advertiser_ids") or [],
            "message": "授权成功，账号已分别入库",
        }
    )
