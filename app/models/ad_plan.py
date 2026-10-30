"""千川计划基础信息。"""

from datetime import datetime
from decimal import Decimal

from sqlalchemy import BigInteger, DateTime, Integer, Numeric, String, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class QianchuanAdPlan(Base):
    __tablename__ = "qianchuan_ad_plan"
    __table_args__ = (UniqueConstraint("advertiser_id", "ad_id", name="uk_adv_ad"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True, comment="主键")
    advertiser_id: Mapped[int] = mapped_column(BigInteger, nullable=False, index=True, comment="千川子账户ID")
    ad_id: Mapped[int] = mapped_column(BigInteger, nullable=False, comment="计划ID")
    ad_name: Mapped[str] = mapped_column(String(512), nullable=False, default="", comment="计划名称")
    roi2_goal: Mapped[Decimal | None] = mapped_column(
        Numeric(10, 4), nullable=True, comment="支付ROI2目标"
    )
    plan_status: Mapped[str] = mapped_column(String(64), nullable=False, default="", comment="计划状态")
    sync_time: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now(), nullable=False, comment="最近同步时间"
    )
