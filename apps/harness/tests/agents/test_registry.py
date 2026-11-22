from pathlib import Path

import pytest

from src.agents import load_agents
from src.core.config import AgentSettings, Settings

GREETER = AgentSettings(
    name="greeter",
    path=Path(__file__).parent / "fixtures" / "greeter" / "profile.yaml",
    family="openai",
    model="gpt-6-luna",
)


def test_the_registry_loads_every_agent_the_settings_list():
    agents = load_agents((GREETER,))

    assert list(agents) == ["greeter"]
    assert (agents["greeter"].family, agents["greeter"].model) == (
        "openai",
        "gpt-6-luna",
    )
    assert agents["greeter"].system_prompt == GREETER.path.parent / "system.md"
    assert agents["greeter"].skills == GREETER.path.parent / "skills"


def test_an_agents_usage_names_the_provider_its_model_family_runs_on():
    providers = [
        load_agents((GREETER.model_copy(update={"family": family}),))[
            "greeter"
        ].model_provider
        for family in ("openai", "claude", "glm")
    ]

    assert providers == ["openai", "anthropic", "gateway"]


def test_an_unknown_model_family_is_refused():
    with pytest.raises(ValueError, match="Input should be 'claude', 'glm' or 'openai'"):
        AgentSettings.model_validate(
            {"name": "greeter", "path": "p.yaml", "family": "gpt", "model": "m"}
        )


def test_config_toml_lists_agents_as_tables(tmp_path: Path, monkeypatch):
    toml = tmp_path / "config.toml"
    toml.write_text(
        '[models]\nopenai = ["gpt-6-luna"]\n\n'
        '[[agents]]\nname = "greeter"\npath = "src/agents/greeter/profile.yaml"\n'
        'family = "openai"\nmodel = "gpt-6-luna"\n'
    )
    monkeypatch.setenv("ZUMI_HARNESS_CONFIG", str(toml))

    [agent] = Settings().agents

    assert agent.name == "greeter"
    assert agent.path.is_absolute()
    assert agent.path.parts[-4:] == ("src", "agents", "greeter", "profile.yaml")


def test_an_agent_on_a_model_the_config_does_not_offer_is_refused(
    tmp_path: Path, monkeypatch
):
    toml = tmp_path / "config.toml"
    toml.write_text(
        '[models]\nopenai = ["gpt-6-luna"]\n\n'
        '[[agents]]\nname = "greeter"\npath = "p.yaml"\n'
        'family = "openai"\nmodel = "gpt-9"\n'
    )
    monkeypatch.setenv("ZUMI_HARNESS_CONFIG", str(toml))

    with pytest.raises(ValueError, match="which \\[models\\] does not offer"):
        Settings()
