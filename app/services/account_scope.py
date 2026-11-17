"""当前授权账号。页面请求和定时任务都先套上它，避免两个账号的数混在一起。"""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.advertiser import QianchuanAdvertiser
from app.models.oauth_token import OAuthToken


@dataclass(frozen=True)
class AccountScope:
    agent_id: int
    kind: str
    name: str


# 澄迈金江栩宁靓商贸店。这家是消耗和总余额同时为 0 才不入库，不按英文括号取舍。
CHENGMAI_AGENT_ID = 1874198362144009

_current: ContextVar[AccountScope | None] = ContextVar("account_scope", default=None)


def current() -> AccountScope | None:
    return _current.get()


def agent_id() -> int | None:
    scope = _current.get()
    return int(scope.agent_id) if scope else None


def is_shop() -> bool:
    scope = _current.get()
    return bool(scope and scope.kind == "shop")


def is_chengmai() -> bool:
    return agent_id() == CHENGMAI_AGENT_ID


def scope_from_token(row: OAuthToken) -> AccountScope:
    return AccountScope(
        agent_id=int(row.agent_id or 0),
        kind=str(row.account_kind or "org"),
        name=str(row.agent_name or ""),
    )


@contextmanager
def activated(row: OAuthToken):
    token = _current.set(scope_from_token(row))
    try:
        yield _current.get()
    finally:
        _current.reset(token)


@contextmanager
def activated_agent(agent: int, kind: str = "org", name: str = ""):
    token = _current.set(AccountScope(agent_id=int(agent), kind=kind or "org", name=name or ""))
    try:
        yield _current.get()
    finally:
        _current.reset(token)


def keeps_name(name: str) -> bool:
    """组织账号仍只留英文括号店。单店、以及澄迈这家，名称里没有运营括号也留。"""
    if is_shop() or is_chengmai():
        return bool((name or "").strip())
    from app.services.advertiser_dashboard import _has_parentheses

    return _has_parentheses(name or "")


def owner_label(name: str) -> str | None:
    if is_shop():
        text = (name or "").strip()
        return text or None
    from app.services.advertiser_dashboard import person_name

    return person_name(name or "")


def scoped_advertiser_ids(db: Session) -> list[int]:
    stmt = select(QianchuanAdvertiser.advertiser_id)
    agent = agent_id()
    if agent:
        stmt = stmt.where(QianchuanAdvertiser.agent_id == int(agent))
    return [int(item) for item in db.scalars(stmt).all()]


def only_this_shop(accounts: list[dict]) -> list[dict]:
    """单店任务只留下这家店。组织账号的工作台结果原样返回。"""
    if not is_shop():
        return accounts
    agent = agent_id()
    if not agent:
        return []
    return [item for item in accounts if int(item.get("advertiser_id") or 0) == int(agent)]
