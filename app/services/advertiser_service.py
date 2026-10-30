"""同步管理账户下全部千川子账户。"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.clients.oceanengine import OceanEngineClient
from app.core.exceptions import AppError
from app.models.advertiser import QianchuanAdvertiser
from app.schemas.advertiser import AdvertiserSyncOut
from app.services import token_store


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
            total_page = int(page_info.get("total_page") or page)
            if page >= total_page or not chunk:
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
        total_page = int(page_info.get("total_page") or page)
        if page >= total_page or not chunk:
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
        total_page = int(page_info.get("total_page") or page)
        if page >= total_page or not chunk:
            break
        page += 1
    return result


async def sync_all_advertisers(db: Session) -> AdvertiserSyncOut:
    row = await token_store.ensure_access_token(db)
    if not row.agent_id:
        raise AppError("尚未绑定 agent_id，请先调用 /api/oauth/fetch-agent-id", code=40412)

    client = await token_store.build_client(db)
    agent_id = int(row.agent_id)
    role = str(row.account_role or "")

    if role.startswith("PLATFORM_ROLE_ENTERPRISE_BP") or role.endswith("ENTERPRISE_BP_ADMIN") or role.endswith(
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
    for item in accounts:
        adv_id = int(item["advertiser_id"])
        existing = db.scalar(
            select(QianchuanAdvertiser).where(QianchuanAdvertiser.advertiser_id == adv_id)
        )
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

    db.commit()
    return AdvertiserSyncOut(
        agent_id=agent_id,
        total=len(accounts),
        created=created,
        updated=updated,
    )
