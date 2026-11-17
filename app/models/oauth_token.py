"""每个授权账号一行。同一个 token 可以绑到多个账号上，刷新时一起更新。"""

from datetime import datetime

from sqlalchemy import BigInteger, DateTime, Integer, SmallInteger, String, Text, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class OAuthToken(Base):
    __tablename__ = "oauth_token"
    __table_args__ = (UniqueConstraint("agent_id", name="uk_oauth_token_agent"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True, comment="主键")
    agent_id: Mapped[int | None] = mapped_column(
        BigInteger, nullable=True, index=True, comment="工作台/代理商管理账户ID"
    )
    agent_name: Mapped[str | None] = mapped_column(String(255), nullable=True, comment="管理账户名称")
    account_role: Mapped[str | None] = mapped_column(
        String(64), nullable=True, comment="授权账户角色，决定用哪套拉子账户接口"
    )
    account_kind: Mapped[str] = mapped_column(
        String(16), nullable=False, default="org", server_default="org", comment="org=组织账户，shop=单店"
    )
    selected: Mapped[int] = mapped_column(
        SmallInteger, nullable=False, default=0, server_default="0", comment="1=当前页面正在看的账号"
    )
    access_token: Mapped[str] = mapped_column(Text, nullable=False, comment="访问令牌")
    refresh_token: Mapped[str] = mapped_column(Text, nullable=False, comment="刷新令牌")
    expires_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, comment="access_token 过期时间")
    refresh_expires_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, comment="refresh_token 过期时间"
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), nullable=False, comment="创建时间"
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now(), nullable=False, comment="更新时间"
    )
