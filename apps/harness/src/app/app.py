from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request

from src.app.agui import router
from src.app.container import Container
from src.infrastructure import (
    configure_logging,
    configure_metrics,
    configure_tracing,
    instrument,
    shutdown_metrics,
    shutdown_tracing,
)
from src.infrastructure.persistence import open_postgres


def health_check(request: Request) -> dict[str, str]:
    """Reports that the server is up, with its version"""
    return {"status": "ok", "version": request.app.version}


@asynccontextmanager
async def _lifespan(container: Container) -> AsyncIterator[None]:
    """Opens Postgres with LangGraph's tables and keeps runs going while the app runs,
    then hands this process's runs over, closes Postgres and flushes telemetry"""
    await open_postgres(
        container.engine(),
        container.pool(),
        container.settings().database.schema_name,
        container.checkpointer(),
    )
    async with container.runner().kept():
        yield
    await container.pool().close()
    await container.engine().dispose()
    await container.upkeep_engine().dispose()
    shutdown_metrics()
    shutdown_tracing()


def create_app(container: Container | None = None) -> FastAPI:
    """Creates the FastAPI app serving the container's agents over AG-UI, with
    logging, tracing and metrics configured"""
    container = container or Container()
    settings = container.settings()
    configure_logging(settings)
    configure_tracing(settings)
    configure_metrics(settings)
    instrument(settings)
    app = FastAPI(
        title=settings.app.name.upper(),
        version=settings.app.version,
        lifespan=lambda _: _lifespan(container),
    )
    app.state.engine = container.engine()
    app.state.jwks = container.jwks()
    app.state.auth = settings.auth
    app.state.agents = {served.name: served for served in container.agents()}
    app.state.runner = container.runner()
    app.add_api_route(path="/health", endpoint=health_check, methods=["GET"])
    app.include_router(router)
    return app
