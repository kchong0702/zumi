import re

import yaml
from fastapi.testclient import TestClient
from langchain_core.messages import AIMessage, HumanMessage

from src.agents import build_agent, load_agents
from src.agents.sothoth.tools import (
    BROWSER_TOOLS,
    exa_search_tool,
    web_search_tool,
)
from src.app.app import create_app
from src.core.config import get_settings
from tests.conftest import Realm, ScriptedChatModel


def test_every_tool_the_prompts_name_is_one_sothoth_has_when_it_reads_them():
    agent = load_agents(get_settings().agents)["sothoth"]
    included = set()

    for skill in agent.skills.glob("*/SKILL.md"):
        _, frontmatter, body = skill.read_text().split("---", 2)
        listed = set(yaml.safe_load(frontmatter)["metadata"]["include_tools"].split())
        assert set(re.findall(r"`(\w+)`", body)) <= BROWSER_TOOLS | listed
        included |= listed

    assert set(re.findall(r"`(\w+)`", agent.system_prompt.read_text())) <= BROWSER_TOOLS
    assert included == {"web_search", "exa_search"}


async def test_sothoth_gets_the_search_tools_only_once_it_reads_find_sources():
    settings = get_settings()
    agent = load_agents(settings.agents)["sothoth"]
    model = ScriptedChatModel(
        replies=[
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "read_file",
                        "args": {"file_path": "/find-sources/SKILL.md"},
                        "id": "call-1",
                    }
                ],
            ),
            AIMessage("Ready to search."),
        ]
    )

    await build_agent(
        agent,
        skill_tools=(
            web_search_tool(settings.search, None),
            exa_search_tool(settings.search, None),
        ),
        model=model,
        recursion_limit=settings.recursion_limit,
    ).ainvoke({"messages": [HumanMessage("Find papers on GUI agents.")]})

    assert "find-sources" in model.requests[0][0].text
    assert {"web_search", "exa_search"}.isdisjoint(model.offered_tools[0])
    assert {"web_search", "exa_search"} <= model.offered_tools[1]


def test_the_app_serves_sothoth_over_ag_ui(realm: Realm):
    response = TestClient(create_app()).post(
        "/sothoth", headers=realm.headers(), json={"messages": "hi"}
    )

    assert response.status_code == 422
    assert {error["loc"][-1] for error in response.json()["detail"]} >= {
        "threadId",
        "runId",
    }
