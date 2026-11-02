"""账户按日数据：回填脚本逐日写入，总览查历史区间直接读库零接口。

字段与子账户页（工作台账户表）同源同口径。
"""

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


class QianchuanAccountDaily(Base):
    __tablename__ = "qianchuan_account_daily"
    __table_args__ = (
        UniqueConstraint("advertiser_id", "stat_date", name="uk_adv_date"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True, comment="主键")
    advertiser_id: Mapped[int] = mapped_column(BigInteger, nullable=False, index=True, comment="千川子账户ID")
    stat_date: Mapped[date] = mapped_column(Date, nullable=False, index=True, comment="统计日期")
    cost: Mapped[Decimal] = mapped_column(Numeric(16, 4), nullable=False, default=0, comment="整体消耗（元）")
    pay_amount: Mapped[Decimal] = mapped_column(
        Numeric(16, 4), nullable=False, default=0, comment="用户实际支付金额（元）"
    )
    pay_roi: Mapped[Decimal | None] = mapped_column(Numeric(16, 4), nullable=True, comment="整体支付ROI")
    settle_amount: Mapped[Decimal | None] = mapped_column(
        Numeric(16, 4), nullable=True, comment="净成交金额（元）"
    )
    settle_roi: Mapped[Decimal | None] = mapped_column(Numeric(16, 4), nullable=True, comment="净成交ROI")
    refund_rate_1h: Mapped[Decimal | None] = mapped_column(
        Numeric(8, 6), nullable=True, comment="1小时内退款率（0~1）"
    )
    sync_time: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now(), nullable=False, comment="最近同步时间"
    )
