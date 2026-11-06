"""团队管理：admin 决定每个账号能看哪些店铺。"""

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.api.deps import get_db
from app.core.response import ok
from app.services.account_access import (
    current_username,
    list_members,
    member_shops,
    require_admin,
    save_visible_shops,
)

router = APIRouter(tags=["Team"])


class VisibleShopsIn(BaseModel):
    advertiser_ids: list[int] = Field(default_factory=list)
    account_operate: bool | None = None
    plan_operate: bool | None = None


@router.get("/team/members", summary="团队成员和各自可见店铺数")
def team_members(request: Request, db: Session = Depends(get_db)):
    username = current_username(request)
    require_admin(username)
    return ok(list_members(db))


@router.get("/team/members/{member}", summary="某个成员的店铺勾选")
def team_member_shops(member: str, request: Request, db: Session = Depends(get_db)):
    require_admin(current_username(request))
    return ok(member_shops(db, member))


@router.put("/team/members/{member}", summary="保存某个成员能看的店铺")
def team_save_shops(
    member: str,
    payload: VisibleShopsIn,
    request: Request,
    db: Session = Depends(get_db),
):
    require_admin(current_username(request))
    return ok(
        save_visible_shops(
            db,
            member,
            payload.advertiser_ids,
            account_operate=payload.account_operate,
            plan_operate=payload.plan_operate,
        )
    )
