from sqlalchemy import create_engine, event
from sqlalchemy.orm import DeclarativeBase, sessionmaker

from .config import get_config

cfg = get_config()

engine = create_engine(
    f"sqlite:///{cfg.DB_PATH}",
    connect_args={"check_same_thread": False, "timeout": 30},
)


@event.listens_for(engine, "connect")
def _set_sqlite_pragma(dbapi_connection, connection_record):
    cursor = dbapi_connection.cursor()
    cursor.execute("PRAGMA journal_mode=WAL")
    cursor.execute("PRAGMA busy_timeout=30000")
    cursor.execute("PRAGMA synchronous=NORMAL")
    cursor.execute("PRAGMA foreign_keys=ON")
    cursor.close()


SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


class Base(DeclarativeBase):
    pass


def init_db():
    from . import models  # noqa: F401 确保模型注册

    Base.metadata.create_all(engine)
    _migrate_add_columns()
    _migrate_coin_transactions_account_nullable()


def _migrate_add_columns():
    """SQLite ALTER TABLE ADD COLUMN 轻量迁移：老库自动补新列。"""
    with engine.begin() as conn:
        _add_column(conn, "accounts",
                    "provider TEXT NOT NULL DEFAULT 'creative-fabrica'", "provider")
        _add_column(conn, "accounts",
                    "capcut_signs_text TEXT NOT NULL DEFAULT ''", "capcut_signs_text")
        _add_column(conn, "generations",
                    "upstream_token TEXT NOT NULL DEFAULT ''", "upstream_token")
        _add_column(conn, "generations",
                    "credit_before FLOAT NOT NULL DEFAULT 0", "credit_before")
        _add_column(conn, "api_keys",
                    "capcut_link_mode TEXT NOT NULL DEFAULT 'official'", "capcut_link_mode")
        _add_column(conn, "generations",
                    "link_mode TEXT NOT NULL DEFAULT ''", "link_mode")
        _add_column(conn, "models",
                    "ref_limits_text TEXT NOT NULL DEFAULT ''", "ref_limits_text")
        _add_column(conn, "models",
                    "gen_limits_text TEXT NOT NULL DEFAULT ''", "gen_limits_text")


def _add_column(conn, table: str, ddl: str, col_name: str):
    cols = conn.exec_driver_sql(f"PRAGMA table_info({table})").mappings().all()
    if not any(c["name"] == col_name for c in cols):
        conn.exec_driver_sql(f"ALTER TABLE {table} ADD COLUMN {ddl}")


def _migrate_coin_transactions_account_nullable():
    """把旧库 coin_transactions.account_id 从 NOT NULL 迁移为可空。

    SQLite 不支持直接 DROP NOT NULL，因此只在检测到旧结构时重建该表。
    流水数据原样复制；新版数据库不会再次执行迁移。
    """
    with engine.connect() as conn:
        columns = conn.exec_driver_sql("PRAGMA table_info(coin_transactions)").mappings().all()
    account_id = next((column for column in columns if column["name"] == "account_id"), None)
    if not account_id or not account_id["notnull"]:
        return

    raw = engine.raw_connection()
    cursor = raw.cursor()
    try:
        # 必须在事务开始前关闭外键检查，才能重命名被引用的表。
        cursor.execute("PRAGMA foreign_keys=OFF")
        cursor.execute("BEGIN")
        cursor.execute("ALTER TABLE coin_transactions RENAME TO coin_transactions_legacy")
        cursor.execute("""
            CREATE TABLE coin_transactions (
                id INTEGER NOT NULL,
                account_id INTEGER,
                account_name TEXT NOT NULL,
                generation_id TEXT NOT NULL,
                kind TEXT NOT NULL,
                before_balance FLOAT NOT NULL,
                cost FLOAT NOT NULL,
                after_balance FLOAT NOT NULL,
                note TEXT NOT NULL,
                created_at FLOAT NOT NULL,
                PRIMARY KEY (id),
                FOREIGN KEY(account_id) REFERENCES accounts (id)
            )
        """)
        cursor.execute("""
            INSERT INTO coin_transactions
                (id, account_id, account_name, generation_id, kind,
                 before_balance, cost, after_balance, note, created_at)
            SELECT id, account_id, account_name, generation_id, kind,
                   before_balance, cost, after_balance, note, created_at
            FROM coin_transactions_legacy
        """)
        cursor.execute("DROP TABLE coin_transactions_legacy")
        cursor.execute("COMMIT")
    except Exception:
        raw.rollback()
        raise
    finally:
        try:
            cursor.execute("PRAGMA foreign_keys=ON")
        finally:
            cursor.close()
            raw.close()


def db_session():
    """FastAPI 依赖。"""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
