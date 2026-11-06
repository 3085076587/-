"""成员在账户管理和智能调控上是仅查看还是可操作。没有记录时按可操作。"""

from datetime import datetime

from sqlalchemy import DateTime, SmallInteger, String, func
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class TeamMemberPerm(Base):
    __tablename__ = "team_member_perm"

    username: Mapped[str] = mapped_column(String(64), primary_key=True, comment="登录账号")
    account_operate: Mapped[int] = mapped_column(
        SmallInteger, nullable=False, default=1, comment="账户管理：1=可操作，0=仅查看"
    )
    plan_operate: Mapped[int] = mapped_column(
        SmallInteger, nullable=False, default=1, comment="智能调控：1=可操作，0=仅查看"
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now(), nullable=False, comment="更新时间"
    )
