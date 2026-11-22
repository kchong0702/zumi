from collections.abc import Awaitable, Callable, Mapping
from typing import Literal

import httpx
from pydantic import BaseModel, ConfigDict, SecretStr, ValidationError

from src.core.config import SearchSettings

ExaCategory = Literal[
    "publication", "company", "people", "financial report", "news", "personal site"
]
"""A kind of page Exa can limit a search to"""


class SearchError(Exception):
    pass


class SearchResult(BaseModel):
    model_config = ConfigDict(
        extra="forbid", frozen=True, use_attribute_docstrings=True
    )

    title: str
    """The page's title"""

    url: str
    """The page's address"""

    text: str
    """The page's text, or only a snippet of it"""

    is_snippet: bool
    """Whether text is only a snippet of the page"""


class _TavilyResult(BaseModel):
    model_config = ConfigDict(
        extra="ignore", frozen=True, use_attribute_docstrings=True
    )

    title: str
    """The page's title"""

    url: str
    """The page's address"""

    content: str
    """A snippet of the page"""

    raw_content: str | None = None
    """The page's text, when Tavily has it"""


class _TavilyAnswer(BaseModel):
    model_config = ConfigDict(
        extra="ignore", frozen=True, use_attribute_docstrings=True
    )

    results: list[_TavilyResult]
    """The pages Tavily found"""


class _ExaResult(BaseModel):
    model_config = ConfigDict(
        extra="ignore", frozen=True, use_attribute_docstrings=True
    )

    title: str
    """The page's title"""

    url: str
    """The page's address"""

    text: str | None = None
    """The page's text, when Exa has it"""


class _ExaAnswer(BaseModel):
    model_config = ConfigDict(
        extra="ignore", frozen=True, use_attribute_docstrings=True
    )

    results: list[_ExaResult]
    """The pages Exa found"""


class _SearxngResult(BaseModel):
    model_config = ConfigDict(
        extra="ignore", frozen=True, use_attribute_docstrings=True
    )

    title: str
    """The page's title"""

    url: str
    """The page's address"""

    content: str = ""
    """A snippet of the page"""


class _SearxngAnswer(BaseModel):
    model_config = ConfigDict(
        extra="ignore", frozen=True, use_attribute_docstrings=True
    )

    results: list[_SearxngResult]
    """The pages SearXNG found"""


async def tavily_results(
    search: SearchSettings,
    api_key: SecretStr | None,
    query: str,
    client: httpx.AsyncClient,
) -> list[SearchResult]:
    """Asks Tavily, which gives each page's text when it has it"""
    if api_key is None:
        raise SearchError("TAVILY_API_KEY is not set")
    response = await client.post(
        f"{search.tavily_url}/search",
        headers={"Authorization": f"Bearer {api_key.get_secret_value()}"},
        json={
            "query": query,
            "max_results": search.max_results,
            "include_raw_content": "markdown",
        },
    )
    response.raise_for_status()
    return [
        SearchResult(
            title=result.title,
            url=result.url,
            text=result.raw_content or result.content,
            is_snippet=not result.raw_content,
        )
        for result in _TavilyAnswer.model_validate_json(response.content).results
    ]


async def exa_results(
    search: SearchSettings,
    api_key: SecretStr | None,
    query: str,
    category: ExaCategory | None,
    client: httpx.AsyncClient,
) -> list[SearchResult]:
    """Asks Exa, which gives each page's text when it has it"""
    if api_key is None:
        raise SearchError("EXA_API_KEY is not set")
    response = await client.post(
        f"{search.exa_url}/search",
        headers={"x-api-key": api_key.get_secret_value()},
        json={
            "query": query,
            "type": "auto",
            "numResults": search.max_results,
            "contents": {"text": True},
        }
        | ({"category": category} if category else {}),
    )
    response.raise_for_status()
    return [
        SearchResult(
            title=result.title,
            url=result.url,
            text=result.text or "",
            is_snippet=not result.text,
        )
        for result in _ExaAnswer.model_validate_json(response.content).results
    ]


async def searxng_results(
    search: SearchSettings, query: str, client: httpx.AsyncClient
) -> list[SearchResult]:
    """Asks SearXNG, which gives a snippet of each page"""
    response = await client.get(
        f"{search.searxng_url}/search", params={"q": query, "format": "json"}
    )
    response.raise_for_status()
    return [
        SearchResult(
            title=result.title, url=result.url, text=result.content, is_snippet=True
        )
        for result in _SearxngAnswer.model_validate_json(response.content).results[
            : search.max_results
        ]
    ]


async def first_results(
    search: SearchSettings,
    services: Mapping[
        str, Callable[[httpx.AsyncClient], Awaitable[list[SearchResult]]]
    ],
) -> tuple[str, list[SearchResult]]:
    """Asks each service in order and returns the name and results of the first
    that answers, raising SearchError that names each failure when none does"""
    failures = []
    async with httpx.AsyncClient(timeout=search.timeout_s) as client:
        for name, results in services.items():
            try:
                return name, await results(client)
            except (httpx.HTTPError, SearchError, ValidationError) as error:
                failures.append(
                    f"{name}: {(str(error) or type(error).__name__).splitlines()[0]}"
                )
    raise SearchError(f"Search failed. {'; '.join(failures)}")


def listed_results(
    search: SearchSettings, query: str, results: list[SearchResult]
) -> str:
    """The results as numbered text, each page's text cut to max_content_chars"""
    entries = []
    for number, result in enumerate(results, start=1):
        text = result.text[: search.max_content_chars]
        if cut := len(result.text) - len(text):
            text += f"\n[cut: {cut} more characters]"
        label = "Snippet" if result.is_snippet else "Page text"
        entries.append(f"{number}. {result.title}\nURL: {result.url}\n{label}:\n{text}")
    return f"Results for: {query}\n\n{'\n\n'.join(entries) or 'No results.'}"
