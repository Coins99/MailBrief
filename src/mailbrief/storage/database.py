"""Async database lifecycle and transaction boundaries."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Self

from sqlalchemy import event
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from mailbrief.storage.tables import Base


def sqlite_url(database_path: Path) -> str:
    """Return an aiosqlite URL for an absolute filesystem path."""
    return f"sqlite+aiosqlite:///{database_path.resolve().as_posix()}"


def _configure_sqlite_connection(
    dbapi_connection: Any,
    _connection_record: Any,
) -> None:
    """Enable integrity and concurrency settings on every SQLite connection."""
    cursor = dbapi_connection.cursor()
    try:
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.execute("PRAGMA busy_timeout=30000")
        cursor.execute("PRAGMA journal_mode=WAL")
    finally:
        cursor.close()


class Database:
    """Own the async engine, sessions, transactions, and shutdown behavior."""

    def __init__(self, url: str, *, echo: bool = False) -> None:
        self._engine = create_async_engine(url, echo=echo)
        self._session_factory = async_sessionmaker(
            self._engine,
            expire_on_commit=False,
        )
        if url.startswith("sqlite+"):
            event.listen(self._engine.sync_engine, "connect", _configure_sqlite_connection)

    @classmethod
    def from_path(cls, database_path: Path, *, echo: bool = False) -> Self:
        """Create a database and ensure its parent data directory exists."""
        database_path.parent.mkdir(parents=True, exist_ok=True)
        return cls(sqlite_url(database_path), echo=echo)

    @property
    def engine(self) -> AsyncEngine:
        """Expose the engine for migrations and diagnostics."""
        return self._engine

    @asynccontextmanager
    async def session(self) -> AsyncIterator[AsyncSession]:
        """Yield a session without an implicit commit."""
        async with self._session_factory() as session:
            yield session

    @asynccontextmanager
    async def transaction(self) -> AsyncIterator[AsyncSession]:
        """Commit a successful unit of work or roll it back on failure."""
        async with self._session_factory() as session, session.begin():
            yield session

    async def create_schema_for_tests(self) -> None:
        """Create current metadata directly for isolated tests only."""
        async with self._engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)

    async def dispose(self) -> None:
        """Release all database connections during application shutdown."""
        await self._engine.dispose()
