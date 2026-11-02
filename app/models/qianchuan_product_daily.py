"""商品按日数据：定时任务逐日写入，总览查历史区间直接读库零接口。"""

from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import (
    BigInteger,
    Date,
    DateTime,
    Integer,
    Numeric,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class QianchuanProductDaily(Base):
    __tablename__ = "qianchuan_product_daily"
    __table_args__ = (
        UniqueConstraint("advertiser_id", "product_id", "stat_date", name="uk_adv_product_date"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True, comment="主键")
    advertiser_id: Mapped[int] = mapped_column(BigInteger, nullable=False, index=True, comment="千川子账户ID")
    product_id: Mapped[int] = mapped_column(BigInteger, nullable=False, index=True, comment="商品ID")
    stat_date: Mapped[date] = mapped_column(Date, nullable=False, comment="统计日期")
    cost: Mapped[Decimal] = mapped_column(Numeric(16, 4), nullable=False, default=0, comment="消耗（元）")
    pay_amount: Mapped[Decimal] = mapped_column(
        Numeric(16, 4), nullable=False, default=0, comment="支付GMV（元）"
    )
    pay_roi: Mapped[Decimal | None] = mapped_column(Numeric(16, 4), nullable=True, comment="支付ROI2")
    settle_amount: Mapped[Decimal | None] = mapped_column(
        Numeric(16, 4), nullable=True, comment="净成交金额（元）"
    )
    settle_roi: Mapped[Decimal | None] = mapped_column(Numeric(16, 4), nullable=True, comment="净成交ROI")
    refund_rate_1h: Mapped[Decimal | None] = mapped_column(
        Numeric(8, 6), nullable=True, comment="1小时内退款率（0~1）"
    )
    order_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0, comment="成交订单数")
    sync_time: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now(), nullable=False, comment="最近同步时间"
    )


class QianchuanProductSync(Base):
    """商品同步标记：记录某账户某天的商品数据已同步（哪怕 0 商品）。

    解决「零商品日」与「未同步日」无法区分的问题：读库覆盖判定看本表，
    而不是看商品日表是否有行，避免为空天反复打接口。
    """

    __tablename__ = "qianchuan_product_sync"
    __table_args__ = (
        UniqueConstraint("advertiser_id", "stat_date", name="uk_adv_date_sync"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True, comment="主键")
    advertiser_id: Mapped[int] = mapped_column(BigInteger, nullable=False, index=True, comment="千川子账户ID")
    stat_date: Mapped[date] = mapped_column(Date, nullable=False, index=True, comment="统计日期")
    product_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0, comment="当天商品行数（0=已同步但无商品）")
    sync_time: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now(), nullable=False, comment="最近同步时间"
    )
