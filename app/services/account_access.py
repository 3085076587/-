"""登录用户能看哪些店铺。admin 看全部。其他人默认看括号里自己的店，团队授权可以加减。"""

from fastapi import Request
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.core.exceptions import AppError
from app.models.advertiser import QianchuanAdvertiser
from app.models.sys_user import SysUser
from app.models.team_member_perm import TeamMemberPerm
from app.models.team_shop_access import TeamShopAccess
from app.services.advertiser_dashboard import _has_parentheses, person_name

ADMIN_USERNAME = "admin"
EFFECT_GRANT = 1
EFFECT_DENY = 0


def current_username(request: Request) -> str:
    user = getattr(request.state, "user", None)
    if not user or not user.get("username"):
        raise AppError("未登录", code=40100)
    return str(user["username"])


def is_admin(username: str) -> bool:
    return username == ADMIN_USERNAME


def require_admin(username: str) -> None:
    if not is_admin(username):
        raise AppError("只有管理员可以管理团队", code=40301)


def _shops(db: Session) -> list[QianchuanAdvertiser]:
    rows = db.scalars(select(QianchuanAdvertiser).order_by(QianchuanAdvertiser.advertiser_id.asc())).all()
    return [row for row in rows if _has_parentheses(row.advertiser_name or "")]


def _overrides(db: Session, username: str) -> dict[int, int]:
    rows = db.scalars(select(TeamShopAccess).where(TeamShopAccess.username == username)).all()
    return {int(row.advertiser_id): int(row.effect) for row in rows}


def shop_visible(owner: str | None, username: str, effect: int | None) -> bool:
    """自己的店默认可见。effect=0 隐藏，effect=1 开放别人的店。"""
    if effect == EFFECT_DENY:
        return False
    if effect == EFFECT_GRANT:
        return True
    return owner == username


def visible_advertiser_ids(db: Session, username: str) -> set[int] | None:
    """admin 返回 None，表示不限制。其他账号返回能看的店铺 ID。"""
    if is_admin(username):
        return None
    overrides = _overrides(db, username)
    visible: set[int] = set()
    for row in _shops(db):
        adv_id = int(row.advertiser_id)
        owner = person_name(row.advertiser_name or "")
        if shop_visible(owner, username, overrides.get(adv_id)):
            visible.add(adv_id)
    return visible


def people_for_visible(db: Session, advertiser_ids: set[int]) -> list[str]:
    """这些店铺分别属于哪些人，用来去拉他们名下已经缓存的账户数据。"""
    if not advertiser_ids:
        return []
    names: list[str] = []
    seen: set[str] = set()
    for row in _shops(db):
        if int(row.advertiser_id) not in advertiser_ids:
            continue
        owner = person_name(row.advertiser_name or "")
        if not owner or owner in seen:
            continue
        seen.add(owner)
        names.append(owner)
    return names


def assert_advertiser_access(db: Session, username: str, advertiser_id: int) -> None:
    allowed = visible_advertiser_ids(db, username)
    if allowed is None:
        return
    if int(advertiser_id) not in allowed:
        raise AppError("没有这家店的查看权限", code=40301)


def member_permissions(db: Session, username: str) -> dict[str, bool]:
    """没有保存过权限时，账户管理和智能调控都按可操作。"""
    row = db.get(TeamMemberPerm, username)
    if row is None:
        return {"account_operate": True, "plan_operate": True}
    return {
        "account_operate": int(row.account_operate) == 1,
        "plan_operate": int(row.plan_operate) == 1,
    }


def assert_can_operate(db: Session, username: str, scope: str) -> None:
    """admin 不受限。scope 为 account 或 plan。"""
    if is_admin(username):
        return
    flags = member_permissions(db, username)
    allowed = flags["plan_operate"] if scope == "plan" else flags["account_operate"]
    if allowed:
        return
    label = "智能调控" if scope == "plan" else "账户管理"
    raise AppError(f"这个账号的{label}是仅查看", code=40301)


def list_members(db: Session) -> list[dict]:
    users = db.scalars(select(SysUser).order_by(SysUser.id.asc())).all()
    total = len(_shops(db))
    members = []
    for user in users:
        if is_admin(user.username):
            continue
        visible = visible_advertiser_ids(db, user.username) or set()
        flags = member_permissions(db, user.username)
        members.append(
            {
                "username": user.username,
                "user_id": int(user.id),
                "shop_count": len(visible),
                "shop_total": total,
                "all_shops": total > 0 and len(visible) == total,
                "account_operate": flags["account_operate"],
                "plan_operate": flags["plan_operate"],
            }
        )
    return members


def member_shops(db: Session, username: str) -> dict:
    user = db.scalar(select(SysUser).where(SysUser.username == username))
    if user is None or is_admin(username):
        raise AppError("没有这个成员", code=40401)
    overrides = _overrides(db, username)
    shops = []
    for row in _shops(db):
        adv_id = int(row.advertiser_id)
        owner = person_name(row.advertiser_name or "") or ""
        shops.append(
            {
                "advertiser_id": adv_id,
                "advertiser_name": row.advertiser_name,
                "owner": owner,
                "owned": owner == username,
                "visible": shop_visible(owner, username, overrides.get(adv_id)),
            }
        )
    flags = member_permissions(db, username)
    return {"username": username, "shops": shops, **flags}


def save_visible_shops(
    db: Session,
    username: str,
    advertiser_ids: list[int],
    *,
    account_operate: bool | None = None,
    plan_operate: bool | None = None,
) -> dict:
    user = db.scalar(select(SysUser).where(SysUser.username == username))
    if user is None or is_admin(username):
        raise AppError("没有这个成员", code=40401)
    wanted = {int(item) for item in advertiser_ids}
    known = {int(row.advertiser_id) for row in _shops(db)}
    unknown = wanted - known
    if unknown:
        raise AppError("有店铺不在账户列表里", code=40001)
    db.execute(delete(TeamShopAccess).where(TeamShopAccess.username == username))
    for row in _shops(db):
        adv_id = int(row.advertiser_id)
        owner = person_name(row.advertiser_name or "")
        want = adv_id in wanted
        owned = owner == username
        if want and not owned:
            db.add(TeamShopAccess(username=username, advertiser_id=adv_id, effect=EFFECT_GRANT))
        elif owned and not want:
            db.add(TeamShopAccess(username=username, advertiser_id=adv_id, effect=EFFECT_DENY))
    if account_operate is not None or plan_operate is not None:
        current = member_permissions(db, username)
        account_flag = current["account_operate"] if account_operate is None else bool(account_operate)
        plan_flag = current["plan_operate"] if plan_operate is None else bool(plan_operate)
        perm = db.get(TeamMemberPerm, username)
        if perm is None:
            perm = TeamMemberPerm(username=username)
            db.add(perm)
        perm.account_operate = 1 if account_flag else 0
        perm.plan_operate = 1 if plan_flag else 0
    db.commit()
    return member_shops(db, username)
