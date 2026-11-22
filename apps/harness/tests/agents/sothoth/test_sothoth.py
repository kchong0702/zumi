import re

from fastapi.testclient import TestClient

from src.agents import load_agents
from src.agents.sothoth.tools import BROWSER_TOOLS
from src.app.app import create_app
from src.core.config import get_settings
from tests.conftest import Realm


def test_every_tool_the_system_prompt_names_is_a_browser_tool():
    agent = load_agents(get_settings().agents)["sothoth"]

    assert set(re.findall(r"`(\w+)`", agent.system_prompt.read_text())) <= BROWSER_TOOLS


def test_the_app_serves_sothoth_over_ag_ui(realm: Realm):
    response = TestClient(create_app()).post(
        "/sothoth", headers=realm.headers(), json={"messages": "hi"}
    )

    assert response.status_code == 422
    assert {error["loc"][-1] for error in response.json()["detail"]} >= {
        "threadId",
        "runId",
    }
