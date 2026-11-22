import pytest
from langchain_core.tools import BaseTool
from pydantic import SecretStr

from src.agents.sothoth.tools import exa_search_tool
from tests.agents.sothoth.tools.conftest import Services

FALLBACK = "1. Notes\nURL: https://example.org/n\nSnippet:\nShort"


@pytest.fixture
def exa_search(services: Services) -> BaseTool:
    return exa_search_tool(services.search, SecretStr("exa-key"))


async def test_exa_search_asks_exa_with_the_category(
    services: Services, exa_search: BaseTool
):
    result = await exa_search.ainvoke(
        {"query": "papers on GUI agents", "category": "publication"}
    )

    assert result.startswith("<search untrusted>\n")
    assert "1. A paper\nURL: https://example.org/paper\nPage text:\nAbstract" in result
    request = services.requests["/exa/search"]
    assert request["headers"]["x-api-key"] == "exa-key"
    assert request["category"] == "publication"


async def test_exa_search_without_a_category_sends_no_category(
    services: Services, exa_search: BaseTool
):
    await exa_search.ainvoke({"query": "papers on GUI agents"})

    assert "category" not in services.requests["/exa/search"]


async def test_searxng_answers_when_exa_is_out_of_credit(
    services: Services, exa_search: BaseTool
):
    services.answers["/exa/search"] = (402, {"error": "out of credit"})

    result = await exa_search.ainvoke({"query": "papers on GUI agents"})

    assert FALLBACK in result


@pytest.mark.parametrize(("status", "service"), [(200, "exa"), (402, "searxng")])
async def test_a_search_names_the_service_that_answered_as_its_artifact(
    services: Services, exa_search: BaseTool, status: int, service: str
):
    services.answers["/exa/search"] = (status, services.answers["/exa/search"][1])

    message = await exa_search.ainvoke(
        {
            "name": "exa_search",
            "args": {"query": "papers"},
            "id": "c",
            "type": "tool_call",
        }
    )

    assert message.artifact == service


async def test_searxng_answers_without_an_exa_key(services: Services):
    result = await exa_search_tool(services.search, None).ainvoke(
        {"query": "papers on GUI agents"}
    )

    assert FALLBACK in result
    assert "/exa/search" not in services.requests
