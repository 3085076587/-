"""巨量引擎工作台登录凭证，整库只保留一条。"""

from datetime import datetime

from sqlalchemy import BigInteger, DateTime, Integer, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class WorkbenchSession(Base):
    __tablename__ = "workbench_session"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True, comment="主键")
    ebpid: Mapped[int] = mapped_column(BigInteger, nullable=False, comment="升级版工作台组织 ID")
    cookie: Mapped[str] = mapped_column(Text, nullable=False, comment="工作台登录 Cookie")
    csrf_token: Mapped[str] = mapped_column(String(255), nullable=False, default="", comment="csrftoken")
    expires_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, comment="预计失效时间，保活成功会顺延")
    last_check_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True, comment="最近一次保活时间")
    last_error: Mapped[str] = mapped_column(String(500), nullable=False, default="", comment="最近一次失败原因")
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now(), nullable=False, comment="更新时间"
    )
