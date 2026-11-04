"""全域商品主档：每个账户下每个商品一行，佣金挂商品维度。"""

from datetime import datetime

from sqlalchemy import (
    BigInteger,
    DateTime,
    Integer,
    Numeric,
    String,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class QianchuanProduct(Base):
    __tablename__ = "qianchuan_product"
    __table_args__ = (
        UniqueConstraint("advertiser_id", "product_id", name="uk_adv_product"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True, comment="主键")
    advertiser_id: Mapped[int] = mapped_column(BigInteger, nullable=False, index=True, comment="千川子账户ID")
    product_id: Mapped[int] = mapped_column(BigInteger, nullable=False, index=True, comment="商品ID")
    product_name: Mapped[str] = mapped_column(String(512), nullable=False, default="", comment="商品标题")
    commission_pct: Mapped[float | None] = mapped_column(
        Numeric(8, 4), nullable=True, comment="商品佣金%，空则按默认 60"
    )
    actual_refund_rate: Mapped[float | None] = mapped_column(
        Numeric(8, 6), nullable=True, comment="用户填写的实际退款率（0~1），空表示未填"
    )
    first_seen_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), nullable=False, comment="首次出现时间"
    )
    last_sync_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now(), nullable=False, comment="最近同步时间"
    )
