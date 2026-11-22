import asyncio

from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool
from sqlalchemy import make_url, text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from src.core.config import DatabaseSettings


def create_engine(settings: DatabaseSettings) -> AsyncEngine:
    """An async SQLAlchemy engine for the harness's own tables, working in its
    schema"""
    return create_async_engine(
        settings.url.get_secret_value(),
        pool_size=settings.pool_size,
        connect_args={"options": f"-c search_path={settings.schema_name}"},
    )


def create_pool(settings: DatabaseSettings) -> AsyncConnectionPool:
    """A psycopg pool for LangGraph's checkpointer, working in the harness's schema;
    it starts closed"""
    return AsyncConnectionPool(
        make_url(settings.url.get_secret_value())
        .set(drivername="postgresql")
        .render_as_string(hide_password=False),
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
