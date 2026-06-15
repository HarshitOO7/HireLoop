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
