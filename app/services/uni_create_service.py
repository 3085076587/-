"""新建商品全域计划：选抖音号、选商品、上传素材、uni_aweme/ad/create。"""

from __future__ import annotations

import hashlib
from typing import Any

from sqlalchemy.orm import Session

from app.clients.oceanengine import OceanEngineApiError
from app.core.exceptions import AppError
from app.schemas.plan import CreateUniPlanIn
from app.services import token_store

_VIDEO_URL = "https://ad.oceanengine.com/open_api/2/file/video/ad/"
_IMAGE_URL = "https://ad.oceanengine.com/open_api/2/file/image/ad/"
_BID_TYPES = {"SMART_BID_CUSTOM", "SMART_BID_CONSERVATIVE"}
_SCHEDULES = {"SCHEDULE_FROM_NOW", "SCHEDULE_START_END"}
_QCPX = {"QCPX_MODE_OFF", "QCPX_MODE_ON"}
_GOALS = {"AD_CONVERT_TYPE_LIVE_PAY_ROI", "AD_CONVERT_TYPE_LIVE_PURE_PAY_ROI"}
_IMAGE_KEYS = {
    "image_id",
    "image_ids",
    "cover_image_id",
    "product_image_id",
    "video_cover_id",
    "card_image_id",
    "promotion_card_image_id",
}
_COVER_WORDS = ("封面", "cover", "商品图", "商品卡", "配图", "image_id", "video_cover")


def _units(text: str) -> int:
    """汉字算 2 个字符，其余算 1 个。"""
    return sum(2 if ord(ch) > 127 else 1 for ch in text)


def _reason(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, list):
        return "；".join(str(item) for item in value if item)
    return str(value).strip()


def _product_id(item: dict[str, Any]) -> int:
    raw = item.get("id") or item.get("product_id") or 0
    try:
        return int(raw)
    except (TypeError, ValueError):
        return 0


async def list_awemes(db: Session, advertiser_id: int) -> list[dict[str, Any]]:
    client = await token_store.build_client(db)
    data = await client.list_uni_awemes(advertiser_id)
    raw_rows = data.get("aweme_id_list") or data.get("aweme_list") or data.get("list") or []
    rows = []
    for item in raw_rows:
        if not isinstance(item, dict):
            continue
        try:
            aweme_id = int(item.get("aweme_id") or 0)
        except (TypeError, ValueError):
            continue
        if not aweme_id:
            continue
        disabled = bool(item.get("is_product_uni_prom_disabled"))
        rows.append(
            {
                "aweme_id": aweme_id,
                "aweme_name": str(item.get("aweme_name") or ""),
                "aweme_show_id": str(item.get("aweme_show_id") or ""),
                "disabled": disabled,
                "reason": _reason(item.get("product_disable_reasons")),
            }
        )
    return rows


async def list_products(
    db: Session,
    advertiser_id: int,
    *,
    aweme_id: int | None,
    keyword: str,
    page: int,
) -> dict[str, Any]:
    client = await token_store.build_client(db)
    try:
        data = await client.list_uni_products(
            advertiser_id,
            aweme_id=aweme_id,
            keyword=keyword,
            page=max(1, page),
        )
    except OceanEngineApiError as exc:
        text = str(exc)
        if not aweme_id and ("aweme" in text.lower() or "抖音" in text):
            raise AppError("这个账户要先选择抖音号，再获取商品") from exc
        raise
    products = []
    for item in data.get("product_list") or []:
        if not isinstance(item, dict):
            continue
        product_id = _product_id(item)
        if not product_id:
            continue
        reason = _reason(item.get("gray_reason"))
        img = item.get("img") or item.get("image_url") or ""
        if not img:
            squares = item.get("square_image_list") or []
            if squares and isinstance(squares[0], dict):
                img = squares[0].get("img_url") or squares[0].get("url") or ""
            elif squares and isinstance(squares[0], str):
                img = squares[0]
        products.append(
            {
                "product_id": str(product_id),
                "name": str(item.get("name") or item.get("product_name") or ""),
                "img": str(img or ""),
                "selectable": not reason,
                "reason": reason,
            }
        )
    page_info = data.get("page_info") or {}
    return {
        "products": products,
        "page": int(page_info.get("page") or page or 1),
        "total_page": int(page_info.get("total_page") or 1),
    }


def _md5(content: bytes) -> str:
    return hashlib.md5(content).hexdigest()


async def upload_video(db: Session, advertiser_id: int, filename: str, content: bytes) -> dict[str, Any]:
    if not content:
        raise AppError("视频是空的")
    if len(content) > 200 * 1024 * 1024:
        raise AppError("视频不能超过 200MB")
    client = await token_store.build_client(db)
    data = await client.upload_ad_file(
        url=_VIDEO_URL,
        form={
            "advertiser_id": advertiser_id,
            "upload_type": "UPLOAD_BY_FILE",
            "video_signature": _md5(content),
        },
        file_field="video_file",
        filename=filename or "video.mp4",
        content=content,
        content_type="video/mp4",
    )
    video_id = str(data.get("video_id") or "")
    if not video_id:
        raise AppError("视频上传成功但没有返回 video_id", data=data)
    width = int(data.get("width") or 0)
    height = int(data.get("height") or 0)
    image_mode = "VIDEO_VERTICAL" if height >= width and height else "VIDEO_LARGE"
    return {
        "video_id": video_id,
        "width": width,
        "height": height,
        "image_mode": image_mode,
        "material_id": data.get("material_id"),
    }


async def upload_image(
    db: Session,
    advertiser_id: int,
    filename: str,
    content: bytes,
    *,
    kind: str,
) -> dict[str, Any]:
    if not content:
        raise AppError("图片是空的")
    limit = 10 * 1024 * 1024 if kind == "cover" else int(1.5 * 1024 * 1024)
    if len(content) > limit:
        raise AppError("封面不能超过 10MB" if kind == "cover" else "商品卡图片不能超过 1.5MB")
    client = await token_store.build_client(db)
    data = await client.upload_ad_file(
        url=_IMAGE_URL,
        form={
            "advertiser_id": advertiser_id,
            "upload_type": "UPLOAD_BY_FILE",
            "image_signature": _md5(content),
        },
        file_field="image_file",
        filename=filename or "image.jpg",
        content=content,
        content_type="image/jpeg",
    )
    image_id = str(data.get("id") or data.get("image_id") or "")
    if not image_id:
        raise AppError("图片上传成功但没有返回图片 ID", data=data)
    return {"image_id": image_id, "width": data.get("width"), "height": data.get("height")}


def _titles(payload: CreateUniPlanIn) -> list[dict[str, str]]:
    title = payload.title.strip()
    units = _units(title)
    if units < 10 or units > 110:
        raise AppError("标题按汉字算 2 个字符，需要 10 到 110 个字符")
    return [{"title": title, "title_type": "CUSTOM"}]


def _drop_image_fields(value: Any) -> Any:
    """请求体里不带封面、商品图或任何图片 ID。"""
    if isinstance(value, dict):
        return {
            key: _drop_image_fields(item)
            for key, item in value.items()
            if key not in _IMAGE_KEYS
        }
    if isinstance(value, list):
        return [_drop_image_fields(item) for item in value]
    return value


def _creative(payload: CreateUniPlanIn, product_id: int) -> dict[str, Any]:
    return {
        "product_id": product_id,
        "creative_type": "PROGRAMMATIC_CREATIVE",
        "video_id": payload.video_id,
        "title_material": _titles(payload),
    }


def _cover_message(message: str, body: dict[str, Any]) -> str | None:
    text = message or ""
    lowered = text.lower()
    if not any(word.lower() in lowered for word in _COVER_WORDS):
        return None
    smart = (body.get("creative_setting") or {}).get("smart_select_material")
    if smart is not True:
        return (
            "接口提示缺少封面，本次请求的 creative_setting.smart_select_material 没有设为 true。"
            "先开启智能优选，不改为要求上传图片。"
        )
    return f"智能优选已经开启（smart_select_material=true），接口仍返回：{text}。没有改为要求上传图片。"


def _build_body(payload: CreateUniPlanIn) -> dict[str, Any]:
    name = payload.name.strip()
    if not (1 <= _units(name) <= 100):
        raise AppError("计划名称按汉字算 2 个字符，需要 1 到 100 个字符")
    if payload.smart_bid_type not in _BID_TYPES:
        raise AppError("投放方式只能是控成本或放量")
    if payload.video_schedule_type not in _SCHEDULES:
        raise AppError("投放时间不正确")
    if payload.qcpx_mode not in _QCPX:
        raise AppError("智能优惠券取值不正确")
    if payload.deep_external_action not in _GOALS:
        raise AppError("优化目标不正确")
    product_ids = []
    for raw in payload.product_ids:
        try:
            product_id = int(raw)
        except (TypeError, ValueError):
            continue
        if product_id and product_id not in product_ids:
            product_ids.append(product_id)
    if not product_ids or len(product_ids) > 30:
        raise AppError("请选择 1 到 30 个商品")
    if not (payload.video_id or "").strip():
        raise AppError("请先上传视频")
    has_aweme = bool(payload.aweme_id)

    delivery: dict[str, Any] = {
        "smart_bid_type": payload.smart_bid_type,
        "budget": round(float(payload.budget), 2),
        "video_schedule_type": payload.video_schedule_type,
        "qcpx_mode": payload.qcpx_mode,
        "deep_external_action": payload.deep_external_action,
    }
    if payload.smart_bid_type == "SMART_BID_CUSTOM":
        if payload.roi2_goal is None or float(payload.roi2_goal) <= 0:
            raise AppError("控成本投放要填写支付 ROI 目标")
        delivery["roi2_goal"] = round(float(payload.roi2_goal), 2)
    if payload.video_schedule_type == "SCHEDULE_START_END":
        if not payload.start_time or not payload.end_time:
            raise AppError("指定投放日期时，开始和结束都要填")
        if payload.end_time < payload.start_time:
            raise AppError("结束日期不能早于开始日期")
        delivery["start_time"] = payload.start_time
        delivery["end_time"] = payload.end_time

    body: dict[str, Any] = {
        "advertiser_id": int(payload.advertiser_id),
        "marketing_goal": "VIDEO_PROM_GOODS",
        "name": name,
        "product_ids": product_ids,
        "delivery_setting": delivery,
        "creative_setting": {"smart_select_material": True},
        "multi_product_creative_list": [_creative(payload, product_id) for product_id in product_ids],
    }
    if has_aweme:
        body["aweme_id"] = int(payload.aweme_id)
    return _drop_image_fields(body)


async def create_plan(db: Session, payload: CreateUniPlanIn) -> dict[str, Any]:
    body = _build_body(payload)
    client = await token_store.build_client(db)
    try:
        data = await client.create_uni_aweme_ad(body)
    except OceanEngineApiError as exc:
        cover = _cover_message(exc.msg, body)
        if cover:
            raise AppError(cover, data={"smart_select_material": True, "ocean_message": exc.msg}) from exc
        raise
    ad_id = data.get("ad_id")
    if not ad_id:
        raise AppError("创建成功但没有返回计划 ID", data=data)
    return {"ad_id": int(ad_id), "name": body["name"]}
