"""商品实际退款率。用户手填，按账户下的商品保存，空表示没填。"""

from sqlalchemy import select
from sqlalchemy.dialects.mysql import insert as mysql_insert
from sqlalchemy.orm import Session

from app.core.exceptions import AppError
from app.models.qianchuan_product import QianchuanProduct


def parse_refund_input(value: object) -> float | None:
    """输入按百分数理解：10 或 10% 都是 10%。0 也算填过。空串表示清空。"""
    if value is None:
        return None
    text = str(value).replace("%", "").strip()
    if text == "":
        return None
    try:
        number = float(text)
    except ValueError as exc:
        raise AppError("实际退款率要填数字", code=40001) from exc
    if number < 0:
        raise AppError("实际退款率不能小于 0", code=40001)
    if number > 1:
        number = number / 100.0
    if number > 1:
        raise AppError("实际退款率不能大于 100%", code=40001)
    return round(number, 6)


def percent_text(ratio: float | None) -> str | None:
    if ratio is None:
        return None
    text = f"{float(ratio) * 100:.4f}".rstrip("0").rstrip(".")
    return text or "0"


def save_product_refund(
    db: Session,
    advertiser_id: int,
    product_id: int,
    product_name: str,
    raw: object,
) -> dict:
    ratio = parse_refund_input(raw)
    name = (product_name or "").strip()
    stmt = mysql_insert(QianchuanProduct).values(
        advertiser_id=int(advertiser_id),
        product_id=int(product_id),
        product_name=name,
        actual_refund_rate=ratio,
    )
    updates = {"actual_refund_rate": stmt.inserted.actual_refund_rate}
    if name:
        updates["product_name"] = stmt.inserted.product_name
    db.execute(stmt.on_duplicate_key_update(**updates))
    db.commit()
    return {
        "advertiser_id": int(advertiser_id),
        "product_id": int(product_id),
        "actual_refund_rate": percent_text(ratio),
    }


def attach_refund_rates(db: Session, advertiser_id: int, plans: list[dict]) -> None:
    """把库里的实际退款率贴到计划的商品上。返回值是百分数文本，方便输入框回填。"""
    product_ids = [int(item["product_id"]) for item in plans if item.get("product_id")]
    saved: dict[int, float | None] = {}
    if product_ids:
        rows = db.scalars(
            select(QianchuanProduct).where(
                QianchuanProduct.advertiser_id == int(advertiser_id),
                QianchuanProduct.product_id.in_(product_ids),
            )
        ).all()
        saved = {int(row.product_id): row.actual_refund_rate for row in rows}
    for item in plans:
        product_id = item.get("product_id")
        ratio = saved.get(int(product_id)) if product_id else None
        item["actual_refund_rate"] = percent_text(None if ratio is None else float(ratio))
