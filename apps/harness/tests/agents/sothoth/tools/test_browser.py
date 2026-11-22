from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from src.agents import build_agent, load_agents
from src.agents.sothoth.tools import BROWSER_TOOLS, open_browser
from src.core.config import BrowserCommand, get_settings
from tests.conftest import ScriptedChatModel


async def test_sothoth_browses_a_page_and_answers_from_it(site: str):
    agent = load_agents(get_settings().agents)["sothoth"]
    model = ScriptedChatModel(
        replies=[
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "browser_navigate",
                        "args": {"url": f"{site}/index.html"},
                        "id": "call-1",
                    }
                ],
            ),
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "browser_snapshot",
                        "args": {"filename": "outline.yml"},
                        "id": "call-2",
                    }
                ],
            ),
            AIMessage(f"The notes are titled Field notes ({site}/index.html)."),
        ]
    )

    async with open_browser(BrowserCommand()) as tools:
        result = await build_agent(
            agent, model=model, recursion_limit=25, tools=tools
        ).ainvoke({"messages": [HumanMessage("What are the field notes called?")]})

    opened, snapshot = [m for m in result["messages"] if isinstance(m, ToolMessage)]
    assert opened.text.startswith("<page untrusted>")
    assert "Page Title: Field notes" in opened.text
    assert snapshot.text.startswith("<page untrusted>")
    assert snapshot.text.lower().count("</page>") == 1
    assert 'link "The paper"' in snapshot.text
    assert {
        name for name in model.offered_tools[0] if name.startswith("browser_")
    } == BROWSER_TOOLS
    assert all((tool.metadata or {})["codename"] == "Traversal" for tool in tools)
    assert result["messages"][-1].text.endswith(f"({site}/index.html).")
