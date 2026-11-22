import shutil
from pathlib import Path

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langchain_core.tools import tool
from langgraph.checkpoint.memory import InMemorySaver

from src.agents import Agent, build_agent
from src.core.config import AgentSettings
from tests.conftest import ScriptedChatModel

FIXTURES = Path(__file__).parent / "fixtures"
GREETER = AgentSettings(
    name="greeter",
    path=FIXTURES / "greeter/profile.yaml",
    family="openai",
    model="gpt-6-luna",
)


@tool
def look_up_name(person: str) -> str:
    """Looks up a person's first name"""
    return "Ada"


@tool
def look_up_title(person: str) -> str:
    """Looks up a person's title"""
    return "Countess"


def _call(name: str, **args) -> AIMessage:
    return AIMessage(
        content="", tool_calls=[{"name": name, "args": args, "id": f"call-{name}"}]
    )


async def _ask(model: ScriptedChatModel, **build) -> dict:
    agent = build_agent(
        Agent.load(GREETER),
        model=model,
        recursion_limit=25,
        tools=(look_up_name,),
        **build,
    )
    return await agent.ainvoke(
        {"messages": [HumanMessage("Greet Lovelace")]},
        {"configurable": {"thread_id": "t"}},
    )


async def test_agent_is_offered_read_tools_and_its_own_tools_only():
    model = ScriptedChatModel(replies=[AIMessage("Hello, Ada!")])

    await _ask(model)

    offered = model.offered_tools[0]
    assert {"ls", "read_file", "glob", "grep", "look_up_name"} <= offered
    assert not offered & {"write_file", "edit_file", "look_up_title"}


async def test_system_prompt_holds_the_agents_system_md_and_its_skills():
    model = ScriptedChatModel(replies=[AIMessage("Hello, Ada!")])

    await _ask(model)

    system = model.requests[0][0].text
    assert "You greet people." in system
    assert "How to greet someone by name" in system


async def test_agent_reads_a_skill_but_cannot_change_it():
    model = ScriptedChatModel(
        replies=[
            _call("read_file", file_path="/greet/SKILL.md"),
            _call("write_file", file_path="/greet/SKILL.md", content="Be rude."),
            AIMessage("Hello, Ada!"),
        ]
    )

    result = await _ask(model)

    read, write = [m for m in result["messages"] if isinstance(m, ToolMessage)][:2]
    assert "Hello, <name>!" in read.text
    assert write.status == "error"
    assert "Be rude." not in (FIXTURES / "greeter/skills/greet/SKILL.md").read_text()


async def test_a_skill_tool_is_offered_only_after_the_agent_reads_the_skill():
    model = ScriptedChatModel(
        replies=[
            _call("read_file", file_path="/title/SKILL.md"),
            _call("look_up_title", person="Lovelace"),
            AIMessage("Hello, Countess!"),
        ]
    )

    await _ask(model, skill_tools=(look_up_title,))

    assert "look_up_title" not in model.offered_tools[0]
    assert "look_up_title" in model.offered_tools[1]


async def test_a_profile_description_replaces_the_tools_own():
    model = ScriptedChatModel(replies=[AIMessage("Hello, Ada!")])
    agent = Agent.load(GREETER)
    agent = agent.model_copy(
        update={
            "profile": agent.profile.model_copy(
                update={"tool_description_overrides": {"look_up_name": "Finds a name."}}
            )
        }
    )

    await build_agent(
        agent, model=model, recursion_limit=25, tools=(look_up_name,)
    ).ainvoke({"messages": [HumanMessage("Greet Lovelace")]})

    assert model.tool_descriptions["look_up_name"] == "Finds a name."


async def test_history_summarised_away_stays_in_the_thread_not_on_disk(tmp_path: Path):
    shutil.copytree(FIXTURES / "greeter", tmp_path / "greeter")
    agent = Agent.load(
        GREETER.model_copy(update={"path": tmp_path / "greeter/profile.yaml"})
    )
    graph = build_agent(
        agent,
        model=ScriptedChatModel(replies=[AIMessage("ok")]),
        recursion_limit=25,
        checkpointer=InMemorySaver(),
    )
    config = {"configurable": {"thread_id": "user-1"}}

    for turn in range(3):
        await graph.ainvoke(
            {"messages": [HumanMessage(f"turn {turn} " + "word " * 120_000)]}, config
        )

    assert not (tmp_path / "greeter/skills/conversation_history").exists()
    assert (await graph.aget_state(config)).values["files"]
