from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.api.deps import get_db
from app.core.response import ok
from app.services import oauth_service

router = APIRouter(tags=["Token"])


@router.get("/token/refresh", summary="刷新 access_token")
async def refresh_token(db: Session = Depends(get_db)):
    """使用 refresh_token 刷新 access_token，并更新 oauth_token 表。"""
    data = await oauth_service.refresh_token(db)
    return ok(data.model_dump())
