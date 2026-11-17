"""同步管理账户下全部千川子账户。"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.clients.oceanengine import OceanEngineClient
from app.core.exceptions import AppError
from app.models.advertiser import QianchuanAdvertiser
from app.models.qianchuan_account_daily import QianchuanAccountDaily
from app.schemas.advertiser import AdvertiserSyncOut
from app.services import account_scope, token_store
from app.services.advertiser_dashboard import _has_parentheses, person_name

logger = logging.getLogger("uvicorn.error")


def _page_has_more(page_info: dict[str, Any], page: int, page_size: int, batch_len: int) -> bool:
    if batch_len <= 0:
        return False
    total_number = int(page_info.get("total_number") or page_info.get("total_num") or 0)
    if total_number > 0:
        return page * page_size < total_number
    total_page = int(page_info.get("total_page") or 0)
    if total_page > 0:
        return page < total_page
    return batch_len >= page_size


def _collect_team_ids(node: dict[str, Any] | list[Any] | None, bag: set[int]) -> None:
    if node is None:
        return
    if isinstance(node, list):
        for item in node:
            _collect_team_ids(item, bag)
        return
    if not isinstance(node, dict):
        return
    team_id = node.get("team_id") or node.get("enterprise_organization_id") or node.get("id")
    if team_id:
        bag.add(int(team_id))
    _collect_team_ids(node.get("sub_team"), bag)
    _collect_team_ids(node.get("children"), bag)


async def _fetch_ebp_accounts(client: OceanEngineClient, root_id: int) -> list[dict[str, Any]]:
    """升级版工作台：根节点 + 全部子团队，分别拉直接挂接的千川账户。"""
    level = await client.get_ebp_level(root_id)
    team_ids = {int(root_id)}
    _collect_team_ids(level.get("sub_team"), team_ids)

    accounts: dict[int, dict[str, Any]] = {}
    for team_id in sorted(team_ids):
        page = 1
        while True:
            data = await client.list_ebp_advertisers(team_id, page=page, page_size=100)
            chunk = data.get("account_list") or data.get("list") or []
            for item in chunk:
                adv_id = int(item.get("account_id") or item.get("advertiser_id") or 0)
                if not adv_id:
                    continue
                accounts[adv_id] = {
                    "advertiser_id": adv_id,
                    "advertiser_name": str(item.get("account_name") or item.get("advertiser_name") or ""),
                    "status": "ACTIVE",
                }
            page_info = data.get("page_info") or {}
            if not _page_has_more(page_info, page, 100, len(chunk)):
                break
            page += 1
    return list(accounts.values())


async def _fetch_agent_accounts(client: OceanEngineClient, agent_id: int) -> list[dict[str, Any]]:
    page = 1
    result: list[dict[str, Any]] = []
    while True:
        data = await client.select_agent_advertisers(agent_id, page=page, page_size=100)
        chunk = data.get("list") or []
        for item in chunk:
            if isinstance(item, dict):
                adv_id = int(item.get("advertiser_id") or item.get("id") or 0)
                name = str(item.get("advertiser_name") or item.get("name") or "")
            else:
                adv_id = int(item)
                name = ""
            if adv_id:
                result.append({"advertiser_id": adv_id, "advertiser_name": name, "status": "ACTIVE"})
        page_info = data.get("page_info") or {}
        if not _page_has_more(page_info, page, 100, len(chunk)):
            break
        page += 1
    return result


async def _fetch_customer_center_accounts(
    client: OceanEngineClient, cc_account_id: int
) -> list[dict[str, Any]]:
    page = 1
    result: list[dict[str, Any]] = []
    while True:
        data = await client.list_customer_center_advertisers(cc_account_id, page=page, page_size=100)
        chunk = data.get("list") or []
        for item in chunk:
            adv_id = int(item.get("advertiser_id") or item.get("account_id") or 0)
            if not adv_id:
                continue
            result.append(
                {
                    "advertiser_id": adv_id,
                    "advertiser_name": str(item.get("advertiser_name") or item.get("account_name") or ""),
                    "status": "ACTIVE",
                }
            )
        page_info = data.get("page_info") or {}
        if not _page_has_more(page_info, page, 100, len(chunk)):
            break
        page += 1
    return result


async def _balance_yuan(client: OceanEngineClient, advertiser_ids: list[int]) -> dict[int, float | None]:
    """总余额，单位元。没查到的是 None，不能当成 0。"""
    found: dict[int, float | None] = {int(adv_id): None for adv_id in advertiser_ids}
    for start in range(0, len(advertiser_ids), 20):
        chunk = [int(adv_id) for adv_id in advertiser_ids[start : start + 20]]
        if not chunk:
            continue
        try:
            data = await client.batch_account_fund(chunk)
        except Exception as exc:  # noqa: BLE001
            logger.warning("查总余额失败，这批先不动：%s", exc)
            continue
        for item in data.get("list") or []:
            adv_id = int(item.get("account_id") or 0)
            if not adv_id:
                continue
            found[adv_id] = float(item.get("balance") or 0) / 100.0
        await asyncio.sleep(0.4)
    return found


def _spent_ids(db: Session, advertiser_ids: list[int]) -> set[int]:
    if not advertiser_ids:
        return set()
    rows = db.scalars(
        select(QianchuanAccountDaily.advertiser_id).where(
            QianchuanAccountDaily.advertiser_id.in_(advertiser_ids),
            QianchuanAccountDaily.cost > 0,
        )
    ).all()
    return {int(item) for item in rows}


async def sync_all_advertisers(db: Session, only_person: str | None = None) -> AdvertiserSyncOut:
    row = await token_store.ensure_access_token(db)
    if not row.agent_id:
        raise AppError("尚未绑定 agent_id，请先调用 /api/oauth/fetch-agent-id", code=40412)

    client = await token_store.build_client(db)
    agent_id = int(row.agent_id)
    role = str(row.account_role or "")
    if str(row.account_kind or "") == "shop":
        accounts = [
            {
                "advertiser_id": agent_id,
                "advertiser_name": row.agent_name or str(agent_id),
                "status": "ACTIVE",
            }
        ]
    elif role.startswith("PLATFORM_ROLE_ENTERPRISE_BP") or role.endswith("ENTERPRISE_BP_ADMIN") or role.endswith(
        "ENTERPRISE_BP_OPERATOR"
    ):
        accounts = await _fetch_ebp_accounts(client, agent_id)
    elif role in {"AGENT", "CHILD_AGENT", "PLATFORM_ROLE_QIANCHUAN_AGENT", "AGENT_SYSTEM_ACCOUNT"}:
        accounts = await _fetch_agent_accounts(client, agent_id)
    elif role in {"CUSTOMER_ADMIN", "CUSTOMER_OPERATOR"}:
        accounts = await _fetch_customer_center_accounts(client, agent_id)
    else:
        # 未知角色：优先按升级版工作台拉，失败再退经典代理商。
        try:
            accounts = await _fetch_ebp_accounts(client, agent_id)
        except Exception:
            accounts = await _fetch_agent_accounts(client, agent_id)

    created = 0
    updated = 0
    now = datetime.now().replace(microsecond=0)
    kept_ids: set[int] = set()
    idle_skip: set[int] = set()
    chengmai = int(row.agent_id or 0) == account_scope.CHENGMAI_AGENT_ID
    if chengmai:
        candidate_ids = [int(item["advertiser_id"]) for item in accounts if int(item.get("advertiser_id") or 0)]
        balances = await _balance_yuan(client, candidate_ids)
        spent = _spent_ids(db, candidate_ids)
        for adv_id in candidate_ids:
            balance = balances.get(adv_id)
            if adv_id in spent or (balance is not None and balance > 0):
                continue
            if balance is not None and balance <= 0:
                idle_skip.add(adv_id)
    for item in accounts:
        adv_id = int(item["advertiser_id"])
        adv_name = item.get("advertiser_name") or ""
        if adv_id in idle_skip:
            continue
        if (
            str(row.account_kind or "") != "shop"
            and not chengmai
            and not _has_parentheses(adv_name)
        ):
            continue
        if only_person is not None and person_name(adv_name) != only_person:
            continue
        existing = db.scalar(
            select(QianchuanAdvertiser).where(QianchuanAdvertiser.advertiser_id == adv_id)
        )
        if existing is not None and int(existing.agent_id) != agent_id:
            continue
        kept_ids.add(adv_id)
        if existing is None:
            db.add(
                QianchuanAdvertiser(
                    agent_id=agent_id,
                    advertiser_id=adv_id,
                    advertiser_name=item.get("advertiser_name") or "",
                    status=item.get("status") or "ACTIVE",
                    sync_time=now,
                )
            )
            created += 1
        else:
            existing.agent_id = agent_id
            if item.get("advertiser_name"):
                existing.advertiser_name = item["advertiser_name"]
            existing.status = item.get("status") or existing.status or "ACTIVE"
            existing.sync_time = now
            updated += 1

    if only_person is None:
        for existing in db.scalars(
            select(QianchuanAdvertiser).where(QianchuanAdvertiser.agent_id == agent_id)
        ).all():
            if int(existing.advertiser_id) in kept_ids:
                continue
            if chengmai:
                if int(existing.advertiser_id) in idle_skip:
                    db.delete(existing)
                continue
            if _has_parentheses(existing.advertiser_name or ""):
                continue
            db.delete(existing)

    db.commit()
    return AdvertiserSyncOut(
        agent_id=agent_id,
        total=len(kept_ids),
        created=created,
        updated=updated,
    )
