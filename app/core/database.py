"""SQLAlchemy 引擎与会话。"""

from collections.abc import Generator

from sqlalchemy import create_engine, text
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from app.core.config import get_settings

settings = get_settings()

engine = create_engine(
    settings.DATABASE_URL,
    pool_pre_ping=True,
    pool_recycle=3600,
)

SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)


class Base(DeclarativeBase):
    pass


# create_all 不会给已存在的表加列，这里声明需兜底补齐的列（表名 -> [(列名, 列定义, 位置)]）。
_REQUIRED_COLUMNS: dict[str, list[tuple[str, str, str]]] = {
    "qianchuan_overview_daily": [
        (
            "refund_rate_1h",
            "DECIMAL(8,6) NULL COMMENT '1小时内退款率（0~1），原站合计'",
            "AFTER settle_roi",
        ),
    ],
    "qianchuan_product_daily": [
        ("settle_roi", "DECIMAL(16,4) NULL COMMENT '净成交ROI'", "AFTER settle_amount"),
        ("refund_rate_1h", "DECIMAL(8,6) NULL COMMENT '1小时内退款率（0~1）'", "AFTER settle_roi"),
    ],
}


def ensure_columns() -> list[str]:
    """幂等补齐旧表缺失的列，返回实际执行的描述列表。"""
    added: list[str] = []
    with engine.begin() as conn:
        for table, columns in _REQUIRED_COLUMNS.items():
            for column, ddl, position in columns:
                exists = conn.execute(
                    text(
                        "SELECT 1 FROM information_schema.columns "
                        "WHERE table_schema = DATABASE() AND table_name = :t AND column_name = :c"
                    ),
                    {"t": table, "c": column},
                ).first()
                if exists:
                    continue
                conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {column} {ddl} {position}"))
                added.append(f"{table}.{column}")
    return added


def get_db() -> Generator[Session, None, None]:
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
