import os
from sqlalchemy import event
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker, AsyncSession
from dotenv import load_dotenv

load_dotenv()

_DATABASE_URL = os.getenv("DATABASE_URL", "sqlite+aiosqlite:///hireloop.db")

# SQLite needs the aiosqlite driver prefix
if _DATABASE_URL.startswith("sqlite:///"):
    _DATABASE_URL = _DATABASE_URL.replace("sqlite:///", "sqlite+aiosqlite:///", 1)

engine = create_async_engine(_DATABASE_URL, echo=False)

# SQLite tuning — applied on every new connection.
# WAL mode is REQUIRED for Litestream replication; without it the DB stays in
# rollback-journal mode and Litestream cannot back up (it errors with
# "attempt to write a readonly database"). WAL is persisted in the DB header,
# so once set it sticks. synchronous=NORMAL is the recommended durability/perf
# balance under WAL; busy_timeout avoids "database is locked" under concurrent
# access (bot + litestream + alembic).
if _DATABASE_URL.startswith("sqlite"):
    @event.listens_for(engine.sync_engine, "connect")
    def _set_sqlite_pragmas(dbapi_conn, _connection_record):
        cursor = dbapi_conn.cursor()
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA synchronous=NORMAL")
        cursor.execute("PRAGMA busy_timeout=5000")
        cursor.close()

AsyncSessionLocal = async_sessionmaker(
    engine,
    class_=AsyncSession,
    expire_on_commit=False,
)


async def get_session() -> AsyncSession:
    async with AsyncSessionLocal() as session:
        yield session


async def ensure_schema_columns(base) -> None:
    """Additive schema reconciliation for SQLite.

    `Base.metadata.create_all` creates MISSING TABLES but never adds a new
    column to a table that already exists. Combined with migrations not being
    deployed (alembic/versions is gitignored), that means any column added to a
    model after a table was first created never reaches the running DB — every
    INSERT then fails with "table X has no column named Y" (this silently broke
    all job saves once `apply_later_at` was added).

    This runs after create_all and adds any model column absent from the live
    table. It is idempotent and safe to run on every startup. Only additive,
    nullable/defaulted columns are handled; a NOT NULL column with no default is
    skipped with a warning (can't be back-filled automatically).
    """
    import logging
    from sqlalchemy import inspect as sa_inspect

    logger = logging.getLogger(__name__)

    def _sync(sync_conn):
        insp = sa_inspect(sync_conn)
        existing_tables = set(insp.get_table_names())
        for table in base.metadata.sorted_tables:
            if table.name not in existing_tables:
                continue  # create_all already handled brand-new tables
            db_cols = {c["name"] for c in insp.get_columns(table.name)}
            for col in table.columns:
                if col.name in db_cols:
                    continue
                if not col.nullable and col.server_default is None and col.default is None:
                    logger.warning(
                        "schema: %s.%s is NOT NULL with no default — cannot add "
                        "automatically; add a manual migration", table.name, col.name)
                    continue
                coltype = col.type.compile(dialect=sync_conn.dialect)
                ddl = f'ALTER TABLE "{table.name}" ADD COLUMN "{col.name}" {coltype}'
                sync_conn.exec_driver_sql(ddl)
                logger.warning("schema: added missing column %s.%s (%s)",
                               table.name, col.name, coltype)

    async with engine.begin() as conn:
        await conn.run_sync(_sync)
