"""数据总览的工作台日汇总。一行是某一天、某一个运营（空字符串表示全部）。

口径和子账户页顶部卡片一样：只加英文括号账户，金额相加，ROI 和订单成本用合计重算。
总利润不放在这张表里。
"""

from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import Date, DateTime, Integer, Numeric, String, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class QianchuanOverviewDaily(Base):
    __tablename__ = "qianchuan_overview_daily"
    __table_args__ = (
        UniqueConstraint("stat_date", "person", name="uk_overview_date_person"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True, comment="主键")
    stat_date: Mapped[date] = mapped_column(Date, nullable=False, index=True, comment="统计日期")
    person: Mapped[str] = mapped_column(
        String(64), nullable=False, default="", comment="运营姓名，空字符串表示全部"
    )
    stat_cost: Mapped[Decimal] = mapped_column(Numeric(16, 4), nullable=False, default=0, comment="整体消耗（元）")
    pay_amount: Mapped[Decimal] = mapped_column(
        Numeric(16, 4), nullable=False, default=0, comment="用户实际支付金额（元）"
    )
    settle_amount: Mapped[Decimal] = mapped_column(
        Numeric(16, 4), nullable=False, default=0, comment="净成交金额（元）"
    )
    order_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0, comment="整体成交订单数")
    pay_roi: Mapped[Decimal | None] = mapped_column(Numeric(16, 4), nullable=True, comment="整体支付ROI")
    settle_roi: Mapped[Decimal | None] = mapped_column(Numeric(16, 4), nullable=True, comment="净成交ROI")
    order_cost: Mapped[Decimal | None] = mapped_column(
        Numeric(16, 4), nullable=True, comment="整体成交订单成本（元）"
    )
    sync_time: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now(), nullable=False, comment="最近同步时间"
    )
