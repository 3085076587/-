"""代理商 OAuth token，整库只保留一条有效记录。"""

from datetime import datetime

from sqlalchemy import BigInteger, DateTime, Integer, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class OAuthToken(Base):
    __tablename__ = "oauth_token"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True, comment="主键")
    agent_id: Mapped[int | None] = mapped_column(
        BigInteger, nullable=True, index=True, comment="工作台/代理商管理账户ID"
    )
    agent_name: Mapped[str | None] = mapped_column(String(255), nullable=True, comment="管理账户名称")
    account_role: Mapped[str | None] = mapped_column(
        String(64), nullable=True, comment="授权账户角色，决定用哪套拉子账户接口"
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
