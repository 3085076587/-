"""登录用户能看哪些子账户。admin 看全部，其他人只看英文括号里的自己的名字。"""

from fastapi import Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.exceptions import AppError
from app.models.advertiser import QianchuanAdvertiser
from app.services.advertiser_dashboard import person_name

ADMIN_USERNAME = "admin"


def current_username(request: Request) -> str:
    user = getattr(request.state, "user", None)
    if not user or not user.get("username"):
        raise AppError("未登录", code=40100)
    return str(user["username"])


def is_admin(username: str) -> bool:
    return username == ADMIN_USERNAME


def assert_advertiser_access(db: Session, username: str, advertiser_id: int) -> None:
    if is_admin(username):
        return
    row = db.scalar(
        select(QianchuanAdvertiser).where(QianchuanAdvertiser.advertiser_id == advertiser_id)
    )
    owner = person_name(row.advertiser_name or "") if row is not None else None
    if owner != username:
        raise AppError("只能操作自己的账户", code=40301)
