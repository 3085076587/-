"""OAuth token 保存、拉取 agent_id、授权回调。"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import HTMLResponse
from sqlalchemy.orm import Session

from app.api.deps import get_db
from app.clients.oceanengine import OceanEngineClient
from app.core.config import get_settings
from app.core.exceptions import AppError, OceanEngineApiError
from app.core.response import ok
from app.schemas.oauth import SaveTokenIn
from app.services import oauth_service, token_store

logger = logging.getLogger("uvicorn.error")
router = APIRouter(tags=["OAuth"])


@router.post("/oauth/save-token", summary="保存 OAuth token")
async def save_token(payload: SaveTokenIn, db: Session = Depends(get_db)):
    """保存 access_token / refresh_token，并立即拉取管理账户 agent_id。"""
    data = await oauth_service.save_token(db, payload)
    agent = await oauth_service.fetch_agent_id(db)
    result = data.model_dump()
    result["agent_id"] = agent.agent_id
    result["agent_name"] = agent.agent_name
    result["account_role"] = agent.account_role
    return ok(result)


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

    row = token_store.save_token(
        db,
        access_token=token["access_token"],
        refresh_token=token["refresh_token"],
        expires_in=int(token.get("expires_in") or 86400),
        refresh_token_expires_in=int(token.get("refresh_token_expires_in") or 2592000),
    )

    # 授权成功后立刻拉管理账户，不再依赖前端手动点。
    agent = await oauth_service.fetch_agent_id(db)

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
    <p>token 已入库，管理账户已自动识别。</p>
    <p>账户：{agent.agent_name or "-"}<br>agent_id：{agent.agent_id}<br>角色：{agent.account_role or "-"}</p>
    <p>2 秒后返回首页，或 <a href="/">立即返回</a>。</p>
  </div>
</body>
</html>"""
        )

    return ok(
        {
            "state": state,
            "token_id": row.id,
            "expires_at": row.expires_at,
            "refresh_expires_at": row.refresh_expires_at,
            "agent_id": agent.agent_id,
            "agent_name": agent.agent_name,
            "account_role": agent.account_role,
            "advertiser_ids": token.get("advertiser_ids") or [],
            "message": "授权成功，token 已入库，agent_id 已自动拉取",
        }
    )
