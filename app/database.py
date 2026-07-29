"""Async engine / session plumbing.

There is no migration tool on purpose (KISS): the schema is created on API
startup. It is a small, additive schema and the only persistent data is room
state, which the app is designed to lose gracefully.
"""

from __future__ import annotations

from sqlalchemy import event
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import StaticPool

from app.models import Base


def normalize_database_url(url: str) -> str:
    """Accept the URLs people actually paste and make them async drivers."""
    if url.startswith("postgres://"):
        url = "postgresql://" + url[len("postgres://") :]
    if url.startswith("postgresql://"):
        url = "postgresql+asyncpg://" + url[len("postgresql://") :]
    if url.startswith("sqlite://") and "+aiosqlite" not in url:
        url = "sqlite+aiosqlite://" + url[len("sqlite://") :]
    return url


def create_engine(url: str, echo: bool = False) -> AsyncEngine:
    url = normalize_database_url(url)
    kwargs: dict = {"echo": echo, "future": True}

    if url.startswith("sqlite"):
        # One shared in-memory database for the whole process, and foreign keys
        # actually enforced -- SQLite is off by default and the tests rely on
        # the cascades behaving like Postgres.
        kwargs["connect_args"] = {"check_same_thread": False}
        kwargs["poolclass"] = StaticPool
    else:
        kwargs["pool_pre_ping"] = True
        kwargs["pool_size"] = 10
        kwargs["max_overflow"] = 10

    engine = create_async_engine(url, **kwargs)

    if url.startswith("sqlite"):

        @event.listens_for(engine.sync_engine, "connect")
        def _enable_foreign_keys(dbapi_connection, _record):  # pragma: no cover - trivial
            cursor = dbapi_connection.cursor()
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.close()

    return engine


def create_sessionmaker(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)


async def create_schema(engine: AsyncEngine) -> None:
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
