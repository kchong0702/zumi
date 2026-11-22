from collections.abc import Callable
from contextlib import AbstractAsyncContextManager

from langgraph.graph.state import CompiledStateGraph
from pydantic import BaseModel, ConfigDict, Field


class ServedAgent(BaseModel):
    model_config = ConfigDict(
        extra="forbid", frozen=True, use_attribute_docstrings=True
    )

    name: str = Field(pattern=r"^[a-z][a-z0-9-]*$")
    """Path segment the agent is served at, such as sothoth"""

    model_provider: str
    """The provider the agent's models run on, which its usage names, such as openai"""

    open_graph: Callable[[], AbstractAsyncContextManager[CompiledStateGraph]]
    """Opens the agent's graph for one run with anything the run holds open, such as a
    browser, and closes them when the run ends"""
