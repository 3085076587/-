"""千川计划按日报表。"""

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


class QianchuanAdReport(Base):
    __tablename__ = "qianchuan_ad_report"
    __table_args__ = (
        UniqueConstraint("advertiser_id", "ad_id", "stat_date", name="uk_adv_ad_date"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True, comment="主键")
    advertiser_id: Mapped[int] = mapped_column(BigInteger, nullable=False, index=True, comment="千川子账户ID")
    ad_id: Mapped[int] = mapped_column(BigInteger, nullable=False, index=True, comment="计划ID")
    stat_date: Mapped[date] = mapped_column(Date, nullable=False, comment="统计日期")
    cost: Mapped[Decimal] = mapped_column(Numeric(16, 4), nullable=False, default=0, comment="消耗")
    pay_order_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0, comment="成交订单数")
    pay_amount: Mapped[Decimal] = mapped_column(
        Numeric(16, 4), nullable=False, default=0, comment="实付/成交金额"
    )
    roi: Mapped[Decimal | None] = mapped_column(Numeric(16, 4), nullable=True, comment="ROI")
    sync_time: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now(), nullable=False, comment="最近同步时间"
    )
