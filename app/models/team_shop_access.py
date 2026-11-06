"""成员能看哪些店铺。没有记录时，只看英文括号里属于自己的店。"""

from datetime import datetime

from sqlalchemy import BigInteger, DateTime, Integer, SmallInteger, String, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class TeamShopAccess(Base):
    __tablename__ = "team_shop_access"
    __table_args__ = (UniqueConstraint("username", "advertiser_id", name="uk_team_shop_user_adv"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True, comment="主键")
    username: Mapped[str] = mapped_column(String(64), nullable=False, index=True, comment="登录账号")
    advertiser_id: Mapped[int] = mapped_column(BigInteger, nullable=False, comment="千川子账户ID")
    effect: Mapped[int] = mapped_column(
        SmallInteger, nullable=False, comment="1=额外授权可见，0=隐藏自己名下的店"
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now(), nullable=False, comment="更新时间"
    )
