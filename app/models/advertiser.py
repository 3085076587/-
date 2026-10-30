"""代理商下属千川子广告账户。"""

from datetime import datetime

from sqlalchemy import BigInteger, DateTime, Integer, String, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class QianchuanAdvertiser(Base):
    __tablename__ = "qianchuan_advertiser"
    __table_args__ = (UniqueConstraint("advertiser_id", name="uk_advertiser_id"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True, comment="主键")
    agent_id: Mapped[int] = mapped_column(BigInteger, nullable=False, index=True, comment="所属代理商ID")
    advertiser_id: Mapped[int] = mapped_column(BigInteger, nullable=False, comment="千川子账户ID")
    advertiser_name: Mapped[str] = mapped_column(
        String(255), nullable=False, default="", comment="账户名称（列表接口仅返回ID时可为空）"
    )
    status: Mapped[str] = mapped_column(String(64), nullable=False, default="UNKNOWN", comment="账户状态")
    sync_time: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now(), nullable=False, comment="最近同步时间"
    )
