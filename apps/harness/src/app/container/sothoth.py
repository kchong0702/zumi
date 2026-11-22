from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from functools import partial

from dependency_injector import containers, providers
from langchain_core.language_models import BaseChatModel
from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.graph.state import CompiledStateGraph

from src.agents import Agent, build_agent
from src.agents.sothoth.tools import open_browser
from src.app.agui import ServedAgent
from src.clients.llm_providers import llm_provider
from src.core.config import BrowserCommand, BrowserServer, Settings


@asynccontextmanager
async def open_sothoth(
    agent: Agent,
    model: Callable[[], BaseChatModel],
    recursion_limit: int,
    checkpointer: Callable[[], BaseCheckpointSaver],
    browser: BrowserCommand | BrowserServer,
) -> AsyncIterator[CompiledStateGraph]:
    """Builds sothoth for one run on a new chat model with checkpointer's current
    saver, and a browser that closes when the run ends"""
    async with open_browser(browser) as browser_tools:
        yield build_agent(
            agent,
            model=model(),
            recursion_limit=recursion_limit,
            tools=browser_tools,
            checkpointer=checkpointer(),
        )


class SothothContainer(containers.DeclarativeContainer):
    settings = providers.Dependency(instance_of=Settings)
    """The app's settings"""

    registry = providers.Dependency(instance_of=dict)
    """Every registered agent, by name"""

    checkpointer = providers.Dependency()
    """Where the agent keeps its conversations"""

    model = providers.Factory(
        llm_provider,
        registry.provided["sothoth"].family,
        registry.provided["sothoth"].model,
        reasoning_effort=settings.provided.reasoning_effort,
        gateway_url=settings.provided.gateway_url,
        gateway_key=settings.provided.gateway_key,
    )
    """A new chat model for sothoth's family and model"""

    served = providers.Singleton(
        ServedAgent,
        name="sothoth",
        model_provider=registry.provided["sothoth"].model_provider,
        open_graph=providers.Factory(
            partial,
            open_sothoth,
            agent=registry.provided["sothoth"],
            model=model.provider,
            recursion_limit=settings.provided.recursion_limit,
            checkpointer=checkpointer.provider,
            browser=settings.provided.browser,
        ),
    )
    """sothoth served over AG-UI, built for each run"""
