"""巨量开放平台 HTTP 客户端。

所有巨量请求统一走这里：打印 URL / 参数 / 返回体；识别 token 过期后由上层刷新再重试。
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any, Callable, Awaitable

import httpx

from app.core.config import Settings, get_settings
from app.core.exceptions import OceanEngineApiError

logger = logging.getLogger("uvicorn.error")

# token 失效才刷新。40100 / 40110 / 40130 是频控，不能当成 token 过期去立刻重试。
TOKEN_EXPIRED_CODES = {40102, 40103, 40104, 40105, 40107}
RATE_LIMIT_CODES = {40100, 40110, 40130}
_rate_lock = asyncio.Lock()
_rate_wait_until = 0.0
_report_lock = asyncio.Lock()
_report_next = 0.0
_REPORT_GAP_SECONDS = 0.4


async def _pace_report() -> None:
    """账户报表两次请求至少隔开 0.4 秒，避免并发把巨量打成 50000。"""
    global _report_next
    async with _report_lock:
        now = time.monotonic()
        wait = max(0.0, _report_next - now)
        _report_next = max(now, _report_next) + _REPORT_GAP_SECONDS
    if wait:
        await asyncio.sleep(wait)


def _message_is_token(message: str) -> bool:
    text = message.lower()
    return "token" in text or "过期" in message or "无效" in message


def _message_is_rate_limit(message: str, ocean_code: int | None) -> bool:
    if any(word in message for word in ("频率", "频控", "限流")):
        return True
    if ocean_code in RATE_LIMIT_CODES and not _message_is_token(message):
        return True
    return False


async def _wait_if_rate_limited() -> None:
    async with _rate_lock:
        delay = _rate_wait_until - time.monotonic()
    if delay > 0:
        await asyncio.sleep(delay)


async def _hold_rate_limit(seconds: float) -> None:
    global _rate_wait_until
    async with _rate_lock:
        _rate_wait_until = max(_rate_wait_until, time.monotonic() + seconds)


class OceanEngineClient:
    def __init__(
        self,
        access_token: str,
        *,
        settings: Settings | None = None,
        token_refresher: Callable[[], Awaitable[str]] | None = None,
    ):
        self.access_token = access_token
        self.settings = settings or get_settings()
        self.token_refresher = token_refresher
        self.base_url = self.settings.OCEANENGINE_API_BASE.rstrip("/")
        self._http_client: httpx.AsyncClient | None = None

    def _http(self) -> httpx.AsyncClient:
        """复用连接。每次新建客户端会把刷新时间耗在握手上。"""
        if self._http_client is None or self._http_client.is_closed:
            self._http_client = httpx.AsyncClient(
                timeout=30.0,
                limits=httpx.Limits(max_connections=20, max_keepalive_connections=10),
            )
        return self._http_client

    def _dump(self, data: object) -> str:
        return json.dumps(data, ensure_ascii=False, default=str)

    async def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json_body: dict[str, Any] | None = None,
        with_token: bool = True,
        retry_on_token_expired: bool = True,
        rate_limit_attempt: int = 0,
        server_attempt: int = 0,
        repeat_lists: bool = False,
    ) -> dict[str, Any]:
        if with_token:
            await _wait_if_rate_limited()
        url = f"{self.base_url}{path}"
        headers: dict[str, str] = {"Content-Type": "application/json"}
        if with_token:
            headers["Access-Token"] = self.access_token

        # 多数 GET 的数组要 JSON 字符串。账户报表 fields 必须拆成重复参数，整段 JSON 会 50000。
        query: list[tuple[str, Any]] | None = None
        if params:
            query = []
            for key, value in params.items():
                if value is None:
                    continue
                if isinstance(value, list) and repeat_lists:
                    for item in value:
                        query.append((key, item))
                elif isinstance(value, (dict, list)):
                    query.append((key, json.dumps(value, ensure_ascii=False, separators=(",", ":"))))
                else:
                    query.append((key, value))

        logger.info(
            "巨量请求 %s %s\nheaders=%s\nparams=%s\nbody=%s",
            method,
            url,
            self._dump({k: ("***" if k == "Access-Token" else v) for k, v in headers.items()}),
            self._dump(query or {}),
            self._dump(json_body or {}),
        )

        response = await self._http().request(
            method,
            url,
            headers=headers,
            params=query,
            json=json_body,
        )

        try:
            body = response.json()
        except ValueError as exc:
            raise OceanEngineApiError(
                f"巨量接口未返回 JSON，HTTP {response.status_code}",
                data={"text": response.text[:800]},
            ) from exc

        logger.info("巨量响应 %s %s\n%s", method, url, self._dump(body))

        if not isinstance(body, dict):
            raise OceanEngineApiError("巨量响应格式异常", data={"body": body})

        code = body.get("code")
        if code not in (0, "0"):
            ocean_code = int(code) if str(code).isdigit() else None
            message = str(body.get("message") or f"巨量接口失败 code={code}")
            if with_token and ocean_code == 50000 and server_attempt < 2:
                wait = server_attempt + 1
                logger.warning("巨量 50000，%s 秒后重试：%s", wait, message)
                await asyncio.sleep(wait)
                return await self._request(
                    method,
                    path,
                    params=params,
                    json_body=json_body,
                    with_token=with_token,
                    retry_on_token_expired=retry_on_token_expired,
                    rate_limit_attempt=rate_limit_attempt,
                    server_attempt=server_attempt + 1,
                    repeat_lists=repeat_lists,
                )
            if (
                with_token
                and rate_limit_attempt < 2
                and _message_is_rate_limit(message, ocean_code)
            ):
                wait = 2 * (rate_limit_attempt + 1)
                logger.warning("巨量频控 code=%s，%s 秒后重试：%s", ocean_code, wait, message)
                await _hold_rate_limit(wait)
                await asyncio.sleep(wait)
                return await self._request(
                    method,
                    path,
                    params=params,
                    json_body=json_body,
                    with_token=with_token,
                    retry_on_token_expired=retry_on_token_expired,
                    rate_limit_attempt=rate_limit_attempt + 1,
                    server_attempt=server_attempt,
                    repeat_lists=repeat_lists,
                )
            token_expired = ocean_code in TOKEN_EXPIRED_CODES or (
                ocean_code == 40100 and _message_is_token(message)
            )
            if (
                retry_on_token_expired
                and with_token
                and token_expired
                and self.token_refresher is not None
            ):
                logger.warning("检测到 token 失效 code=%s，尝试刷新后重试", ocean_code)
                self.access_token = await self.token_refresher()
                return await self._request(
                    method,
                    path,
                    params=params,
                    json_body=json_body,
                    with_token=with_token,
                    retry_on_token_expired=False,
                    rate_limit_attempt=rate_limit_attempt,
                    server_attempt=server_attempt,
                    repeat_lists=repeat_lists,
                )
            raise OceanEngineApiError(
                message,
                ocean_code=ocean_code,
                data=body,
            )
        return body

    async def exchange_access_token(self, auth_code: str) -> dict[str, Any]:
        if not self.settings.OCEANENGINE_APP_ID.isdigit():
            raise OceanEngineApiError("OCEANENGINE_APP_ID 必须是数字")
        body = await self._request(
            "POST",
            "/oauth2/access_token/",
            json_body={
                "app_id": int(self.settings.OCEANENGINE_APP_ID),
                "secret": self.settings.OCEANENGINE_SECRET,
                "grant_type": "auth_code",
                "auth_code": auth_code,
            },
            with_token=False,
            retry_on_token_expired=False,
        )
        data = body.get("data") or {}
        if not data.get("access_token"):
            raise OceanEngineApiError("换 token 响应缺少 access_token", data=body)
        return data

    async def refresh_token(self, refresh_token: str) -> dict[str, Any]:
        if not self.settings.OCEANENGINE_APP_ID.isdigit():
            raise OceanEngineApiError("OCEANENGINE_APP_ID 必须是数字")
        body = await self._request(
            "POST",
            "/oauth2/refresh_token/",
            json_body={
                "app_id": int(self.settings.OCEANENGINE_APP_ID),
                "secret": self.settings.OCEANENGINE_SECRET,
                "grant_type": "refresh_token",
                "refresh_token": refresh_token,
            },
            with_token=False,
            retry_on_token_expired=False,
        )
        data = body.get("data") or {}
        if not data.get("access_token"):
            raise OceanEngineApiError("刷新 token 响应缺少 access_token", data=body)
        return data

    async def get_authorized_advertisers(self) -> dict[str, Any]:
        """GET /oauth2/advertiser/get/ 获取当前 token 下账号，用于识别代理商 agent_id。"""
        body = await self._request(
            "GET",
            "/oauth2/advertiser/get/",
            params={"access_token": self.access_token},
        )
        return body.get("data") or {}

    async def select_agent_advertisers(
        self,
        agent_id: int,
        *,
        page: int = 1,
        page_size: int = 100,
    ) -> dict[str, Any]:
        """GET /2/agent/advertiser/select/ 拉取经典代理商下子账户 ID 列表。

        注意：该接口入参字段名是 advertiser_id，实际传代理商 ID。
        """
        body = await self._request(
            "GET",
            "/2/agent/advertiser/select/",
            params={
                "advertiser_id": agent_id,
                "page": page,
                "page_size": page_size,
            },
        )
        return body.get("data") or {}

    async def get_ebp_level(self, enterprise_organization_id: int) -> dict[str, Any]:
        """GET /2/ebp/level/get/ 拉取升级版工作台组织树。"""
        body = await self._request(
            "GET",
            "/2/ebp/level/get/",
            params={"enterprise_organization_id": enterprise_organization_id},
        )
        return body.get("data") or {}

    async def list_ebp_advertisers(
        self,
        enterprise_organization_id: int,
        *,
        page: int = 1,
        page_size: int = 100,
        account_source: str = "QIANCHUAN",
    ) -> dict[str, Any]:
        """GET /2/ebp/advertiser/list/ 拉取某个 EBP 节点下直接挂接的千川账户。"""
        body = await self._request(
            "GET",
            "/2/ebp/advertiser/list/",
            params={
                "enterprise_organization_id": enterprise_organization_id,
                "account_source": account_source,
                "page": page,
                "page_size": page_size,
            },
        )
        return body.get("data") or {}

    async def list_customer_center_advertisers(
        self,
        cc_account_id: int,
        *,
        page: int = 1,
        page_size: int = 100,
        account_source: str = "QIANCHUAN",
    ) -> dict[str, Any]:
        """GET /2/customer_center/advertiser/list/ 旧版工作台下千川账户。"""
        body = await self._request(
            "GET",
            "/2/customer_center/advertiser/list/",
            params={
                "cc_account_id": cc_account_id,
                "account_source": account_source,
                "page": page,
                "page_size": page_size,
            },
        )
        return body.get("data") or {}

    async def list_ads(
        self,
        advertiser_id: int,
        *,
        page: int = 1,
        page_size: int = 100,
        filtering: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """获取账户下计划列表。

        官方路径是 /v1.0/qianchuan/ad/get/（不是 ad/list）。
        """
        params: dict[str, Any] = {
            "advertiser_id": advertiser_id,
            "page": page,
            "page_size": page_size,
        }
        if filtering:
            params["filtering"] = filtering
        body = await self._request(
            "GET",
            "/v1.0/qianchuan/ad/get/",
            params=params,
        )
        return body.get("data") or {}

    async def get_ad_report(
        self,
        advertiser_id: int,
        start_date: str,
        end_date: str,
        *,
        page: int = 1,
        page_size: int = 100,
        fields: list[str] | None = None,
        filtering: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """GET /v1.0/qianchuan/report/ad/get/ 计划报表。"""
        default_fields = [
            "stat_cost",
            "pay_order_count",
            "pay_order_amount",
            "prepay_and_pay_order_roi",
            "create_order_roi",
        ]
        params: dict[str, Any] = {
            "advertiser_id": advertiser_id,
            "start_date": start_date,
            "end_date": end_date,
            "fields": fields or default_fields,
            "time_granularity": "TIME_GRANULARITY_DAILY",
            "page": page,
            "page_size": page_size,
            "filtering": filtering
            or {
                "marketing_goal": "ALL",
            },
        }
        body = await self._request(
            "GET",
            "/v1.0/qianchuan/report/ad/get/",
            params=params,
        )
        return body.get("data") or {}

    async def update_roi2_goal(
        self,
        advertiser_id: int,
        ad_id: int,
        roi2_goal: float,
        *,
        deep_external_action: str | None = "AD_CONVERT_TYPE_LIVE_PURE_PAY_ROI",
    ) -> dict[str, Any]:
        """修改计划 ROI2 目标（默认净成交 ROI）。

        优先 /v1.0/qianchuan/uni_promotion/ad/roi2_goal/update/，
        失败再试旧路径。
        """
        info: dict[str, Any] = {
            "ad_id": ad_id,
            "roi2_goal": round(float(roi2_goal), 2),
        }
        if deep_external_action:
            info["deep_external_action"] = deep_external_action
        payload = {
            "advertiser_id": advertiser_id,
            "update_roi2_infos": [info],
        }
        try:
            body = await self._request(
                "POST",
                "/v1.0/qianchuan/uni_promotion/ad/roi2_goal/update/",
                json_body=payload,
            )
            return body.get("data") or body
        except OceanEngineApiError as exc:
            logger.warning("uni_promotion roi2 更新失败，尝试旧路径：%s", exc)
            body = await self._request(
                "POST",
                "/v1.0/qianchuan/ad/roi2_goal/update/",
                json_body=payload,
            )
            return body.get("data") or body

    async def update_uni_promotion_status(
        self,
        advertiser_id: int,
        ad_ids: list[int],
        opt_status: str,
    ) -> dict[str, Any]:
        """POST /v1.0/qianchuan/uni_promotion/ad/status/update/ 启停全域计划。"""
        body = await self._request(
            "POST",
            "/v1.0/qianchuan/uni_promotion/ad/status/update/",
            json_body={
                "advertiser_id": advertiser_id,
                "ad_ids": ad_ids,
                "opt_status": opt_status,
            },
        )
        return body.get("data") or body

    async def batch_account_fund(
        self,
        account_ids: list[int],
        *,
        account_type: str = "QIANCHUAN",
    ) -> dict[str, Any]:
        """GET /v3.0/account/fund/get/ 代理商批量查余额（对齐方舟）。金额单位：分。"""
        if not account_ids:
            return {"list": []}
        if len(account_ids) > 20:
            raise OceanEngineApiError("account_ids 单次最多 20 个")
        body = await self._request(
            "GET",
            "/v3.0/account/fund/get/",
            params={
                "account_ids": account_ids,
                "account_type": account_type,
            },
        )
        return body.get("data") or {}

    async def get_qianchuan_balance(self, advertiser_id: int) -> dict[str, Any]:
        """GET /v1.0/qianchuan/account/balance/get/ 含共享子钱包名称/余额。金额单位：千分之一分。"""
        body = await self._request(
            "GET",
            "/v1.0/qianchuan/account/balance/get/",
            params={"advertiser_id": advertiser_id},
        )
        return body.get("data") or {}

    async def get_shared_wallet_info(
        self,
        account_id: int,
        wallet_id_list: list[int],
        *,
        account_type: str = "AD",
    ) -> dict[str, Any]:
        """GET /v3.0/shared_wallet/wallet_info/get/ 批量查钱包名称。"""
        if not wallet_id_list:
            return {"wallet_info_list": []}
        body = await self._request(
            "GET",
            "/v3.0/shared_wallet/wallet_info/get/",
            params={
                "account_id": account_id,
                "wallet_id_list": wallet_id_list,
                "account_type": account_type,
            },
        )
        return body.get("data") or {}

    async def get_shared_wallet_budget(
        self,
        account_id: int,
        sub_wallet_id: int,
        *,
        account_type: str = "AD",
    ) -> dict[str, Any]:
        """GET /v3.0/shared_wallet/budget/get/ 子钱包日预算。"""
        body = await self._request(
            "GET",
            "/v3.0/shared_wallet/budget/get/",
            params={
                "account_id": account_id,
                "account_type": account_type,
                "sub_wallet_id": sub_wallet_id,
            },
        )
        return body.get("data") or {}

    async def get_uni_promotion_report(
        self,
        advertiser_id: int,
        start_date: str,
        end_date: str,
        *,
        fields: list[str] | None = None,
        marketing_goal: str | None = None,
    ) -> dict[str, Any]:
        """GET /v1.0/qianchuan/report/uni_promotion/get/ 全域账户维度。"""
        await _pace_report()
        default_fields = [
            "stat_cost",
            "total_pay_order_count_for_roi2",
            "total_pay_order_gmv_for_roi2",
            "total_prepay_and_pay_order_roi2",
            "total_cost_per_pay_order_for_roi2",
        ]
        params: dict[str, Any] = {
            "advertiser_id": advertiser_id,
            "start_date": start_date,
            "end_date": end_date,
            "fields": fields or default_fields,
        }
        if marketing_goal:
            params["marketing_goal"] = marketing_goal
        body = await self._request(
            "GET",
            "/v1.0/qianchuan/report/uni_promotion/get/",
            params=params,
            repeat_lists=True,
        )
        return body.get("data") or {}

    async def list_uni_promotion(
        self,
        advertiser_id: int,
        start_time: str,
        end_time: str,
        *,
        marketing_goal: str,
        fields: list[str] | None = None,
        page: int = 1,
        page_size: int = 50,
    ) -> dict[str, Any]:
        """GET /v1.0/qianchuan/uni_promotion/list/ 全域计划列表（含 stats_info 消耗指标）。

        金额类字段单位为千分之一分；ROI 为比值。
        marketing_goal: VIDEO_PROM_GOODS / LIVE_PROM_GOODS
        """
        default_fields = [
            "stat_cost",
            "total_pay_order_count_for_roi2",
            "total_pay_order_gmv_for_roi2",
            "total_prepay_and_pay_order_roi2",
            "total_cost_per_pay_order_for_roi2",
        ]
        body = await self._request(
            "GET",
            "/v1.0/qianchuan/uni_promotion/list/",
            params={
                "advertiser_id": advertiser_id,
                "start_time": start_time,
                "end_time": end_time,
                "marketing_goal": marketing_goal,
                "fields": fields or default_fields,
                "page": page,
                "page_size": page_size,
            },
        )
        return body.get("data") or {}
