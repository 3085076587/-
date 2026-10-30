"""系统登录 / 登出（与巨量 OAuth 授权无关）。"""

from fastapi import APIRouter, Depends, Request, Response
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.api.deps import get_db
from app.core.exceptions import AppError
from app.core.response import ok
from app.services import session_auth

router = APIRouter(prefix="/auth", tags=["Auth"])


class LoginBody(BaseModel):
    username: str = Field(..., min_length=1, max_length=64)
    password: str = Field(..., min_length=1, max_length=128)


@router.post("/login", summary="账号密码登录")
async def login(body: LoginBody, response: Response, db: Session = Depends(get_db)):
    if not session_auth.verify_credentials(db, body.username, body.password):
        raise AppError("账号或密码错误", code=40101)
    session_auth.set_session_cookie(response, body.username.strip())
    return ok({"username": body.username.strip()}, msg="登录成功")


@router.post("/logout", summary="退出登录")
async def logout(response: Response):
    session_auth.clear_session_cookie(response)
    return ok(None, msg="已退出")


@router.get("/me", summary="当前登录用户")
async def me(request: Request):
    user = session_auth.read_session(request)
    if not user:
        raise AppError("未登录", code=40100)
    return ok(user)
