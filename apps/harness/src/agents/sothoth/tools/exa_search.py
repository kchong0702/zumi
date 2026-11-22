from functools import partial, update_wrapper

from langchain_core.tools import BaseTool, StructuredTool, ToolException
from pydantic import ConfigDict, Field, SecretStr, create_model

from src.agents.sothoth.untrusted import untrusted
from src.clients.search import (
    ExaCategory,
    SearchError,
    exa_results,
    first_results,
    listed_results,
    searxng_results,
)
from src.core.config import SearchSettings


async def _exa_search(
    search: SearchSettings,
    exa_api_key: SecretStr | None,
    query: str,
    category: ExaCategory | None = None,
) -> tuple[str, str]:
    """Searches the web by meaning: describe the page you want in plain words. Best for
    research papers, companies, people and exploring a topic"""
    try:
        service, results = await first_results(
            search,
            {
                "exa": partial(exa_results, search, exa_api_key, query, category),
                "searxng": partial(searxng_results, search, query),
            },
        )
    except SearchError as error:
        raise ToolException(str(error)) from error
    return untrusted("search", listed_results(search, query, results)), service


def exa_search_tool(search: SearchSettings, exa_api_key: SecretStr | None) -> BaseTool:
    """exa_search, which asks Exa with exa_api_key and falls back to SearXNG"""
    return StructuredTool.from_function(
        coroutine=update_wrapper(
            partial(_exa_search, search, exa_api_key), _exa_search
        ),
        name="exa_search",
        args_schema=create_model(
            "ExaSearchArgs",
            __config__=ConfigDict(extra="forbid"),
            query=(
                str,
                Field(description="The page you want, described in plain words"),
            ),
            category=(
                ExaCategory | None,
                Field(
                    default=None, description="Limits the search to one kind of page"
                ),
            ),
        ),
        response_format="content_and_artifact",
        handle_tool_error=True,
        metadata={"codename": "Augury"},
    )
