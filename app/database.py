"""Async engine / session plumbing.

There is no migration tool on purpose (KISS): the schema is created on API
startup. It is a small, additive schema and the only persistent data is room
state, which the app is designed to lose gracefully.

``create_all`` only creates tables it cannot find, though, so a release that
adds a column to an existing one would leave a live database without it — and
every query naming that column then fails. ``reconcile_columns`` closes exactly
that gap and nothing wider: it adds missing columns that carry a default, and
refuses anything it cannot do safely. Renames, drops and type changes are still
not migrations this can do, and are not changes this schema makes.
"""

from __future__ import annotations

import logging

from sqlalchemy import Connection, event, inspect, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import StaticPool
from sqlalchemy.schema import Column, Table

from app.models import Base

logger = logging.getLogger(__name__)


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


def _literal(value: object) -> str | None:
    """A default an ``ALTER TABLE`` can carry, or None if we should not guess.

    Deliberately narrow: booleans, numbers and short strings are every default
    this schema uses, and anything else is a reason to leave the column alone
    and say so rather than to invent SQL.
    """
    if isinstance(value, bool):
        return "TRUE" if value else "FALSE"
    if isinstance(value, int | float):
        return str(value)
    if isinstance(value, str):
        return "'" + value.replace("'", "''") + "'"
    return None


def _add_column_sql(connection: Connection, table: Table, column: Column) -> str | None:
    """DDL for one missing column, or None if adding it is not safe.

    A column with no default cannot be added to a table with rows in it unless
    it is nullable — existing rows have nothing to put there.
    """
    default = column.default.arg if column.default is not None and column.default.is_scalar else None
    literal = _literal(default)

    if literal is None and not column.nullable:
        return None

    type_sql = column.type.compile(connection.dialect)
    sql = f"ALTER TABLE {table.name} ADD COLUMN {column.name} {type_sql}"
    if literal is not None:
        sql += f" DEFAULT {literal}"
        if not column.nullable:
            sql += " NOT NULL"
    return sql


def _reconcile(connection: Connection) -> list[str]:
    """Add every column the models declare and the database does not have."""
    inspector = inspect(connection)
    added: list[str] = []

    for table in Base.metadata.sorted_tables:
        if not inspector.has_table(table.name):
            continue  # create_all just made it, with every column
        present = {column["name"] for column in inspector.get_columns(table.name)}

        for column in table.columns:
            if column.name in present:
                continue
            sql = _add_column_sql(connection, table, column)
            if sql is None:
                logger.warning(
                    "column %s.%s is missing and cannot be added automatically "
                    "(no default, not nullable)",
                    table.name,
                    column.name,
                )
                continue
            connection.execute(text(sql))
            added.append(f"{table.name}.{column.name}")

    return added


async def create_schema(engine: AsyncEngine) -> None:
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    try:
        async with engine.begin() as conn:
            added = await conn.run_sync(_reconcile)
    except SQLAlchemyError:
        # A database that will not take an additive column is a database an
        # operator has to look at. Starting up and failing loudly on the first
        # query is more useful than refusing to start at all.
        logger.exception("could not reconcile the schema")
        return

    if added:
        logger.info("added missing column(s): %s", ", ".join(added))
