"""简易登录会话：HMAC 签名 Cookie。账号在 sys_user，密码只存哈希。"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
from typing import Any

from fastapi import Request, Response
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.models.sys_user import SysUser
from app.services.password_hash import verify_password

COOKIE_NAME = "ad_helper_session"


def _b64e(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _b64d(text: str) -> bytes:
    pad = "=" * (-len(text) % 4)
    return base64.urlsafe_b64decode(text + pad)


def _sign(payload_b64: str) -> str:
    secret = get_settings().SESSION_SECRET.encode("utf-8")
    digest = hmac.new(secret, payload_b64.encode("ascii"), hashlib.sha256).digest()
    return _b64e(digest)


def create_session_token(username: str) -> str:
    settings = get_settings()
    payload = {
        "u": username,
        "exp": int(time.time()) + int(settings.SESSION_MAX_AGE_SECONDS),
    }
    payload_b64 = _b64e(json.dumps(payload, separators=(",", ":")).encode("utf-8"))
    return f"{payload_b64}.{_sign(payload_b64)}"


def parse_session_token(token: str | None) -> dict[str, Any] | None:
    if not token or "." not in token:
        return None
    payload_b64, sig = token.split(".", 1)
    if not hmac.compare_digest(_sign(payload_b64), sig):
        return None
    try:
        payload = json.loads(_b64d(payload_b64).decode("utf-8"))
    except Exception:
        return None
    if int(payload.get("exp") or 0) < int(time.time()):
        return None
    username = str(payload.get("u") or "")
    if not username:
        return None
    return {"username": username}


def verify_credentials(db: Session, username: str, password: str) -> bool:
    name = username.strip()
    if not name or not password:
        return False
    user = db.scalar(select(SysUser).where(SysUser.username == name))
    if user is None:
        return False
    return verify_password(password, user.password_hash)


def read_session(request: Request) -> dict[str, Any] | None:
    return parse_session_token(request.cookies.get(COOKIE_NAME))


def set_session_cookie(response: Response, username: str) -> None:
    settings = get_settings()
    response.set_cookie(
        key=COOKIE_NAME,
        value=create_session_token(username),
        max_age=int(settings.SESSION_MAX_AGE_SECONDS),
        httponly=True,
        samesite="lax",
        path="/",
        secure=False,
    )


def clear_session_cookie(response: Response) -> None:
    response.delete_cookie(key=COOKIE_NAME, path="/")
