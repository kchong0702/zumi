from dependency_injector import containers, providers
from jwt import PyJWKClient
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver

from src.agents import load_agents
from src.app.agui import Runner
from src.app.container.sothoth import SothothContainer
from src.core.config import get_settings
from src.infrastructure.persistence import create_engine, create_pool


class Container(containers.DeclarativeContainer):
    settings = providers.Singleton(get_settings)
    """The app's settings"""

    registry = providers.Singleton(load_agents, settings.provided.agents)
    """Every registered agent, by name"""

    engine = providers.Singleton(
        create_engine,
        settings.provided.database,
        settings.provided.database.pool_size,
    )
    """The engine on the harness's own tables"""

    upkeep_engine = providers.Singleton(
        create_engine,
        settings.provided.database,
        settings.provided.database.upkeep_pool_size,
    )
    """The engine that marks runs alive, sweeps and hands runs over, apart from the
    one requests and workers use"""

    pool = providers.Singleton(create_pool, settings.provided.database)
    """The connection pool LangGraph's checkpointer uses"""

    checkpointer = providers.Singleton(AsyncPostgresSaver, pool)
    """Where agents keep each user's lifelong conversation; made inside the running
    event loop, as AsyncPostgresSaver must be"""

    jwks = providers.Singleton(PyJWKClient, settings.provided.auth.jwks_url)
    """The realm's signing keys, fetched once and again only for an unknown key or
    every five minutes"""

    sothoth = providers.Container(
        SothothContainer,
        settings=settings,
        registry=registry,
        checkpointer=checkpointer,
    )
    """sothoth, the research agent"""

    agents = providers.List(sothoth.served)
    """Agents served over AG-UI"""

    runner = providers.Singleton(
        Runner,
        engine=engine,
        upkeep=upkeep_engine,
        database=settings.provided.database,
        settings=settings.provided.runs,
        agents=agents,
    )
    """What works on the agents' runs in this process and takes over stopped ones"""
