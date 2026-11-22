from typing import Literal

from langchain_anthropic import ChatAnthropic
from langchain_core.language_models import BaseChatModel
from langchain_openai import ChatOpenAI
from pydantic import HttpUrl, SecretStr

from src.core.config import Family


def llm_provider(
    family: Family,
    name: str,
    *,
    reasoning_effort: Literal["low", "medium", "high"],
    gateway_url: HttpUrl | None,
    gateway_key: SecretStr | None,
) -> BaseChatModel:
    """The chat model for an exact model name of a family; claude and openai models
    reason at reasoning_effort and summarise it, glm goes through the gateway"""
    if family == "claude":
        return ChatAnthropic(
            model_name=name,
            thinking={"type": "adaptive", "display": "summarized"},
            reasoning_effort=reasoning_effort,
        )
    if family == "openai":
        return ChatOpenAI(
            model=name,
            use_responses_api=True,
            reasoning={"effort": reasoning_effort, "summary": "auto"},
        )
    if gateway_url is None or gateway_key is None:
        raise ValueError(
            f"{family} models need the gateway: set AIGW_BASE_URL and AIGW_API_KEY"
        )
    return ChatOpenAI(
        model=name,
        base_url=str(gateway_url),
        api_key=gateway_key,
        use_responses_api=False,
        stream_usage=True,
    )
