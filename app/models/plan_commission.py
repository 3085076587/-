"""计划佣金比例。按子账户 + 计划保存，下次打开直接用来算利润。"""

from datetime import datetime
from decimal import Decimal

from sqlalchemy import BigInteger, DateTime, Integer, Numeric, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class PlanCommission(Base):
    __tablename__ = "plan_commission"
    __table_args__ = (UniqueConstraint("advertiser_id", "ad_id", name="uk_plan_commission_adv_ad"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True, comment="主键")
    advertiser_id: Mapped[int] = mapped_column(BigInteger, nullable=False, index=True, comment="千川子账户ID")
    ad_id: Mapped[int] = mapped_column(BigInteger, nullable=False, comment="计划ID")
    commission_pct: Mapped[Decimal] = mapped_column(
        Numeric(8, 2), nullable=False, comment="佣金比例，单位百分数，例如 20 表示 20%"
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now(), nullable=False, comment="更新时间"
    )
