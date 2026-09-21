import json
import logging

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
    _migrate_rebrand_model_ids()


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


# 对外模型名脱敏（2026-09-21 运营要求：模型名不得出现上游品牌）。
# 旧库里的 model_id 长这样：capcut-seedance-2.0 / capcut-seedance_2.5 / capcut-nano_banana
# 新库统一为：          sd-seedance-2.0    / sd-seedance-2.5      / sd-nano_banana
# 注意 2.5 历史上用了**下划线**，不能只做简单前缀替换。迁移是幂等的（改完就没有 capcut- 前缀了）。
_REBRAND_EXPLICIT = {
    "capcut-seedance_2.5": "sd-seedance-2.5",
}


def _rebrand(mid: str) -> str:
    if not mid or not mid.startswith("capcut-"):
        return mid
    if mid in _REBRAND_EXPLICIT:
        return _REBRAND_EXPLICIT[mid]
    return "sd-" + mid[len("capcut-"):]


def _migrate_rebrand_model_ids():
    """把存量 `capcut-*` 的对外模型名改名为 `sd-*`。

    涉及三处，缺一不可：
      - models.model_id            —— 模型目录本身
      - generations.model_id       —— 历史/在跑任务回显（轮询响应里的 model 字段）
      - api_keys.enabled_models_text —— Key 的模型白名单；漏改会让带白名单的 Key 直接
        「一个模型都没有」（本次实测踩到：白名单里写着旧名 → /v1/models 返回空列表）
    """
    with engine.begin() as conn:
        rows = conn.exec_driver_sql("SELECT id, model_id FROM models").mappings().all()
        renamed = 0
        for r in rows:
            new = _rebrand(r["model_id"])
            if new == r["model_id"]:
                continue
            exists = conn.exec_driver_sql(
                "SELECT id FROM models WHERE model_id = ?", (new,)).first()
            if exists:  # 目标名已被占用（重复行），把旧行删掉避免重名
                conn.exec_driver_sql("DELETE FROM models WHERE id = ?", (r["id"],))
            else:
                conn.exec_driver_sql(
                    "UPDATE models SET model_id = ? WHERE id = ?", (new, r["id"]))
            renamed += 1
        gens = 0
        for r in conn.exec_driver_sql(
                "SELECT id, model_id FROM generations").mappings().all():
            new = _rebrand(r["model_id"])
            if new != r["model_id"]:
                conn.exec_driver_sql(
                    "UPDATE generations SET model_id = ? WHERE id = ?", (new, r["id"]))
                gens += 1
        keys = 0
        for r in conn.exec_driver_sql(
                "SELECT id, enabled_models_text FROM api_keys").mappings().all():
            raw = (r["enabled_models_text"] or "").strip()
            if not raw or raw == "[]":
                continue
            try:
                allow = json.loads(raw)
            except Exception:  # noqa: BLE001 —— 脏数据不动
                continue
            if not isinstance(allow, list):
                continue
            new_allow = [_rebrand(x) if isinstance(x, str) else x for x in allow]
            if new_allow != allow:
                conn.exec_driver_sql(
                    "UPDATE api_keys SET enabled_models_text = ? WHERE id = ?",
                    (json.dumps(new_allow, ensure_ascii=False), r["id"]))
                keys += 1
    if renamed or gens or keys:
        logging.getLogger(__name__).warning(
            "模型名脱敏迁移：models %d 条 / generations %d 条 / api_keys %d 条",
            renamed, gens, keys)


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
