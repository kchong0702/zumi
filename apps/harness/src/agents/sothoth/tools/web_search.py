from functools import partial, update_wrapper

from langchain_core.tools import BaseTool, StructuredTool, ToolException
from pydantic import ConfigDict, Field, SecretStr, create_model

from src.agents.sothoth.untrusted import untrusted
from src.clients.search import (
    SearchError,
    first_results,
    listed_results,
    searxng_results,
    tavily_results,
)
from src.core.config import SearchSettings


async def _web_search(
    search: SearchSettings, tavily_api_key: SecretStr | None, query: str
) -> tuple[str, str]:
    """Searches the web by keywords. Best for facts, news, recent events and exact names,
    versions or error messages"""
    try:
        service, results = await first_results(
            search,
            {
                "tavily": partial(tavily_results, search, tavily_api_key, query),
                "searxng": partial(searxng_results, search, query),
            },
        )
    except SearchError as error:
        raise ToolException(str(error)) from error
    return untrusted("search", listed_results(search, query, results)), service


def web_search_tool(
    search: SearchSettings, tavily_api_key: SecretStr | None
) -> BaseTool:
    """web_search, which asks Tavily with tavily_api_key and falls back to SearXNG"""
    return StructuredTool.from_function(
        coroutine=update_wrapper(
            partial(_web_search, search, tavily_api_key), _web_search
        ),
        name="web_search",
        args_schema=create_model(
            "WebSearchArgs",
            __config__=ConfigDict(extra="forbid"),
            query=(str, Field(description="The keywords to search for")),
        ),
        response_format="content_and_artifact",
        handle_tool_error=True,
        metadata={"codename": "Scrying"},
    )
