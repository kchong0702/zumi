from pathlib import Path
from typing import Self

import yaml
from pydantic import BaseModel, ConfigDict, Field

from src.core.config import AgentSettings, Family


class AgentProfile(BaseModel):
    model_config = ConfigDict(
        extra="forbid", frozen=True, use_attribute_docstrings=True
    )

    tool_description_overrides: dict[str, str] = Field(default_factory=dict)
    """Descriptions that replace a tool's own, by tool name"""


class Agent(BaseModel):
    model_config = ConfigDict(
        extra="forbid", frozen=True, use_attribute_docstrings=True
    )

    name: str
    """Agent name, such as sothoth"""

    folder: Path
    """Holds profile.yaml, system.md and the agent's own skills/"""

    family: Family
    """Model family the model belongs to, such as openai"""

    model: str
    """Exact model the agent runs on, such as gpt-6-luna"""

    profile: AgentProfile
    """The agent's own settings, from its profile.yaml"""

    @property
    def model_provider(self) -> str:
        """The provider the agent's model family runs on: openai, anthropic for claude
        or gateway for glm"""
        return {"openai": "openai", "claude": "anthropic", "glm": "gateway"}[
            self.family
        ]

    @property
    def system_prompt(self) -> Path:
        """The agent's system prompt file"""
        return self.folder / "system.md"

    @property
    def skills(self) -> Path:
        """The agent's skills folder"""
        return self.folder / "skills"

    @classmethod
    def load(cls, settings: AgentSettings) -> Self:
        """Reads an agent's profile.yaml for the model and family its settings name"""
        return cls(
            name=settings.name,
            folder=settings.path.parent,
            family=settings.family,
            model=settings.model,
            profile=AgentProfile.model_validate(
                yaml.safe_load(settings.path.read_text())
            ),
        )


def load_agents(agents: tuple[AgentSettings, ...]) -> dict[str, Agent]:
    """Every agent the settings list, by name"""
    return {settings.name: Agent.load(settings) for settings in agents}
