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
    "qianchuan_product": [
        (
            "actual_refund_rate",
            "DECIMAL(8,6) NULL COMMENT '用户填写的实际退款率（0~1），空表示未填'",
            "AFTER commission_pct",
        ),
    ],
    "plan_commission": [
        (
            "filled_by_user",
            "TINYINT NOT NULL DEFAULT 0 COMMENT '1=用户在插件或页面手填，0=名称同步'",
            "AFTER commission_pct",
        ),
    ],
    "qianchuan_product_daily": [
        ("settle_roi", "DECIMAL(16,4) NULL COMMENT '净成交ROI'", "AFTER settle_amount"),
        ("refund_rate_1h", "DECIMAL(8,6) NULL COMMENT '1小时内退款率（0~1）'", "AFTER settle_roi"),
    ],
}


def _column_exists(conn, table: str, column: str) -> bool:
    return (
        conn.execute(
            text(
                "SELECT 1 FROM information_schema.columns "
                "WHERE table_schema = DATABASE() AND table_name = :t AND column_name = :c"
            ),
            {"t": table, "c": column},
        ).first()
        is not None
    )


def _index_exists(conn, table: str, index: str) -> bool:
    return (
        conn.execute(
            text(
                "SELECT 1 FROM information_schema.statistics "
                "WHERE table_schema = DATABASE() AND table_name = :t AND index_name = :i"
            ),
            {"t": table, "i": index},
        ).first()
        is not None
    )


def ensure_columns() -> list[str]:
    """幂等补齐旧表缺失的列，返回实际执行的描述列表。"""
    added: list[str] = []
    with engine.begin() as conn:
        for table, columns in _REQUIRED_COLUMNS.items():
            for column, ddl, position in columns:
                if _column_exists(conn, table, column):
                    continue
                conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {column} {ddl} {position}"))
                added.append(f"{table}.{column}")
    return added


def ensure_account_schema() -> list[str]:
    """多授权账号需要的列和唯一键。已有总览行归到当前这条授权上，不改金额。"""
    done: list[str] = []
    with engine.begin() as conn:
        if not _column_exists(conn, "oauth_token", "account_kind"):
            conn.execute(
                text(
                    "ALTER TABLE oauth_token ADD COLUMN account_kind VARCHAR(16) "
                    "NOT NULL DEFAULT 'org' COMMENT 'org=组织账户，shop=单店' AFTER account_role"
                )
            )
            done.append("oauth_token.account_kind")
        if not _column_exists(conn, "oauth_token", "selected"):
            conn.execute(
                text(
                    "ALTER TABLE oauth_token ADD COLUMN selected TINYINT NOT NULL DEFAULT 0 "
                    "COMMENT '1=当前页面正在看的账号' AFTER account_kind"
                )
            )
            done.append("oauth_token.selected")
        selected = conn.execute(text("SELECT id FROM oauth_token WHERE selected = 1 LIMIT 1")).first()
        if selected is None:
            conn.execute(
                text(
                    "UPDATE oauth_token SET selected = 1 "
                    "WHERE id = (SELECT id FROM (SELECT id FROM oauth_token ORDER BY id DESC LIMIT 1) t)"
                )
            )
            done.append("oauth_token.selected=1")
        if not _index_exists(conn, "oauth_token", "uk_oauth_token_agent"):
            conn.execute(text("ALTER TABLE oauth_token ADD UNIQUE KEY uk_oauth_token_agent (agent_id)"))
            done.append("uk_oauth_token_agent")
        if not _index_exists(conn, "workbench_session", "uk_workbench_ebpid"):
            conn.execute(text("ALTER TABLE workbench_session ADD UNIQUE KEY uk_workbench_ebpid (ebpid)"))
            done.append("uk_workbench_ebpid")
        if not _column_exists(conn, "qianchuan_overview_daily", "agent_id"):
            conn.execute(
                text(
                    "ALTER TABLE qianchuan_overview_daily ADD COLUMN agent_id BIGINT NULL "
                    "COMMENT '授权账号 agent_id' AFTER id"
                )
            )
            done.append("qianchuan_overview_daily.agent_id")
        agent = conn.execute(
            text("SELECT agent_id FROM oauth_token WHERE selected = 1 AND agent_id IS NOT NULL LIMIT 1")
        ).first()
        if agent is None:
            agent = conn.execute(
                text("SELECT agent_id FROM oauth_token WHERE agent_id IS NOT NULL ORDER BY id LIMIT 1")
            ).first()
        if agent is not None and agent[0] is not None:
            result = conn.execute(
                text("UPDATE qianchuan_overview_daily SET agent_id = :agent WHERE agent_id IS NULL"),
                {"agent": int(agent[0])},
            )
            if result.rowcount:
                done.append(f"overview.agent_id={int(agent[0])} rows={result.rowcount}")
        pending = conn.execute(
            text("SELECT COUNT(*) FROM qianchuan_overview_daily WHERE agent_id IS NULL")
        ).scalar()
        if not pending and _index_exists(conn, "qianchuan_overview_daily", "uk_overview_date_person"):
            conn.execute(text("ALTER TABLE qianchuan_overview_daily DROP INDEX uk_overview_date_person"))
            done.append("drop uk_overview_date_person")
        if not pending and not _index_exists(conn, "qianchuan_overview_daily", "uk_overview_agent_date_person"):
            conn.execute(
                text(
                    "ALTER TABLE qianchuan_overview_daily "
                    "ADD UNIQUE KEY uk_overview_agent_date_person (agent_id, stat_date, person)"
                )
            )
            done.append("uk_overview_agent_date_person")
        if not pending:
            nullable = conn.execute(
                text(
                    "SELECT IS_NULLABLE FROM information_schema.columns "
                    "WHERE table_schema = DATABASE() AND table_name = 'qianchuan_overview_daily' "
                    "AND column_name = 'agent_id'"
                )
            ).scalar()
            if nullable == "YES":
                conn.execute(
                    text(
                        "ALTER TABLE qianchuan_overview_daily MODIFY agent_id BIGINT NOT NULL "
                        "COMMENT '授权账号 agent_id'"
                    )
                )
                done.append("overview.agent_id not null")
    return done


def get_db() -> Generator[Session, None, None]:
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
