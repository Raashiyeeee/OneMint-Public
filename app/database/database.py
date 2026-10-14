"""
Async SQLAlchemy 2.x database engine and session factory.
"""
from __future__ import annotations

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.database.models import Base

_engine: AsyncEngine | None = None
_session_factory: async_sessionmaker[AsyncSession] | None = None


def init_engine(database_url: str) -> AsyncEngine:
    """Create and cache the async database engine."""
    global _engine, _session_factory
    _engine = create_async_engine(
        database_url,
        echo=False,
        future=True,
        connect_args={"check_same_thread": False} if "sqlite" in database_url else {},
    )
    _session_factory = async_sessionmaker(
        _engine, expire_on_commit=False, class_=AsyncSession
    )
    return _engine


def get_engine() -> AsyncEngine:
    if _engine is None:
        raise RuntimeError("Database engine not initialised. Call init_engine() first.")
    return _engine


def get_session_factory() -> async_sessionmaker[AsyncSession]:
    if _session_factory is None:
        raise RuntimeError("Session factory not initialised. Call init_engine() first.")
    return _session_factory


async def create_tables() -> None:
    """Create all tables if they don't already exist, then apply light migrations."""
    engine = get_engine()
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        await conn.run_sync(_ensure_notification_kind_column)


def _ensure_notification_kind_column(sync_conn) -> None:
    """Add notifications.kind on existing SQLite DBs created before dual alerts."""
    dialect = sync_conn.dialect.name
    if dialect != "sqlite":
        # create_all handles new installs; non-SQLite deployments should migrate explicitly.
        return
    rows = sync_conn.exec_driver_sql("PRAGMA table_info(notifications)").fetchall()
    columns = {row[1] for row in rows}
    if "kind" not in columns:
        sync_conn.exec_driver_sql(
            "ALTER TABLE notifications ADD COLUMN kind VARCHAR(32) NOT NULL DEFAULT 'reminder'"
        )


async def dispose_engine() -> None:
    """Dispose the engine and release all connections."""
    if _engine is not None:
        await _engine.dispose()
