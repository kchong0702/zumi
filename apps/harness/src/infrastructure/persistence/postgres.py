import asyncio
from collections.abc import AsyncIterator

from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from psycopg import AsyncConnection, sql
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool
from sqlalchemy import make_url, text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from src.core.config import DatabaseSettings
from src.infrastructure.persistence.tables import RunChannel


def _psycopg_url(settings: DatabaseSettings) -> str:
    """The database URL in the form psycopg itself takes"""
    return (
        make_url(settings.url.get_secret_value())
        .set(drivername="postgresql")
        .render_as_string(hide_password=False)
    )


def create_engine(settings: DatabaseSettings, pool_size: int) -> AsyncEngine:
    """An async SQLAlchemy engine for the harness's own tables, working in its
    schema and keeping pool_size connections open"""
    return create_async_engine(
        settings.url.get_secret_value(),
        pool_size=pool_size,
        connect_args={"options": f"-c search_path={settings.schema_name}"},
    )


def create_pool(settings: DatabaseSettings) -> AsyncConnectionPool:
    """A psycopg pool for LangGraph's checkpointer, working in the harness's schema;
    it starts closed"""
    return AsyncConnectionPool(
        _psycopg_url(settings),
        min_size=1,
        max_size=settings.pool_size,
        open=False,
        check=AsyncConnectionPool.check_connection,
        kwargs={
            "autocommit": True,
            "prepare_threshold": 0,
            "row_factory": dict_row,
            "options": f"-c search_path={settings.schema_name}",
        },
    )


async def open_postgres(
    engine: AsyncEngine,
    pool: AsyncConnectionPool,
    schema_name: str,
    checkpointer: AsyncPostgresSaver,
) -> None:
    """Opens the pool and creates the schema and LangGraph's tables, one process
    at a time, holding no transaction that LangGraph's index builds wait on"""
    await pool.open(wait=True)
    async with engine.connect() as connection:
        lock = await connection.execution_options(isolation_level="AUTOCOMMIT")
        while not await lock.scalar(
            text("SELECT pg_try_advisory_lock(hashtext('harness setup'))")
        ):
            await asyncio.sleep(0.5)
        try:
            async with engine.begin() as ddl:
                await ddl.execute(text(f'CREATE SCHEMA IF NOT EXISTS "{schema_name}"'))
            await checkpointer.setup()
        finally:
            await lock.execute(
                text("SELECT pg_advisory_unlock(hashtext('harness setup'))")
            )


async def run_notifications(
    settings: DatabaseSettings, keepalive_s: int
) -> AsyncIterator[tuple[RunChannel, str]]:
    """Each notification on the harness schema's run channels, as its channel and run
    id, on a connection of its own probed every keepalive_s; raises when it fails"""
    async with await AsyncConnection.connect(
        _psycopg_url(settings),
        autocommit=True,
        options=f"-c search_path={settings.schema_name}",
        keepalives=1,
        keepalives_idle=keepalive_s,
        keepalives_interval=keepalive_s,
        keepalives_count=3,
    ) as connection:
        schema = await (await connection.execute("SELECT current_schema()")).fetchone()
        channels = {f"{schema[0]}.{channel}": channel for channel in RunChannel}
        for name in channels:
            await connection.execute(sql.SQL("LISTEN {}").format(sql.Identifier(name)))
        async for notification in connection.notifies():
            yield channels[notification.channel], notification.payload
