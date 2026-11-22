import time
from collections.abc import Mapping
from itertools import chain
from typing import override
from uuid import UUID

import httpx
from ag_ui.core import TokenUsage
from ag_ui.core.token_usage import (
    aggregate_token_usage,
    token_usage_from_langchain_metadata,
)
from langchain_core.callbacks import AsyncCallbackHandler
from langchain_core.messages import AIMessage, BaseMessage, ToolMessage
from langchain_core.outputs import ChatGeneration, LLMResult
from langgraph.errors import GraphRecursionError

from src.infrastructure.persistence import ModelUsage, RunErrorCode, ToolUsage


def _label(metadata: Mapping[str, object] | None, key: str) -> str | None:
    """A callback's run metadata label, such as ls_model_name, when it is set"""
    value = (metadata or {}).get(key)
    return value if isinstance(value, str) else None


class RunUsage(AsyncCallbackHandler):
    def __init__(self, agent: str, provider: str) -> None:
        """Counts the tokens and tool calls of one run of agent, whose models run on
        provider, and keeps the error that ended it"""
        self.agent = agent
        self.provider = provider
        self._error: BaseException | None = None
        self._models: dict[UUID, tuple[str, str | None]] = {}
        self._tokens: list[tuple[str, TokenUsage]] = []
        self._tools: dict[UUID, tuple[str, str, float]] = {}
        self._tool_usage: dict[tuple[str, str, str | None], ToolUsage] = {}
        self._tool_errors: list[BaseException] = []

    def _count_tool(self, run_id: UUID, provider: str | None, failed: bool) -> None:
        """Adds the tool call that ended to its tool's usage"""
        agent, tool, started = self._tools.pop(run_id)
        key = (agent, tool, provider)
        usage = self._tool_usage.get(
            key,
            ToolUsage(
                agent=agent,
                tool=tool,
                provider=provider,
                calls=0,
                failures=0,
                duration_ms=0,
            ),
        )
        self._tool_usage[key] = usage.model_copy(
            update={
                "calls": usage.calls + 1,
                "failures": usage.failures + failed,
                "duration_ms": usage.duration_ms
                + round((time.monotonic() - started) * 1000),
            }
        )

    @override
    async def on_chat_model_start(
        self,
        serialized: dict[str, object],
        messages: list[list[BaseMessage]],
        *,
        run_id: UUID,
        metadata: dict[str, object] | None = None,
        **kwargs: object,
    ) -> None:
        """Notes which agent and model the call is for"""
        self._models[run_id] = (
            _label(metadata, "lc_agent_name") or self.agent,
            _label(metadata, "ls_model_name"),
        )

    @override
    async def on_llm_end(
        self, response: LLMResult, *, run_id: UUID, **kwargs: object
    ) -> None:
        """Counts the tokens the model reported for the call"""
        agent, model = self._models.pop(run_id)
        for generation in chain.from_iterable(response.generations):
            if isinstance(generation, ChatGeneration) and isinstance(
                generation.message, AIMessage
            ):
                usage = token_usage_from_langchain_metadata(
                    generation.message.usage_metadata,
                    provider=self.provider,
                    model=model,
                )
                if usage is not None:
                    self._tokens.append((agent, usage))

    @override
    async def on_llm_error(
        self, error: BaseException, *, run_id: UUID, **kwargs: object
    ) -> None:
        """Forgets the failed call"""
        self._models.pop(run_id, None)

    @override
    async def on_tool_start(
        self,
        serialized: dict[str, object],
        input_str: str,
        *,
        run_id: UUID,
        metadata: dict[str, object] | None = None,
        **kwargs: object,
    ) -> None:
        """Notes which agent called which tool, and when"""
        self._tools[run_id] = (
            _label(metadata, "lc_agent_name") or self.agent,
            str(serialized["name"]),
            time.monotonic(),
        )

    @override
    async def on_tool_end(
        self, output: object, *, run_id: UUID, **kwargs: object
    ) -> None:
        """Counts the call, as failed when it returned an error to the model; a
        tool's string artifact names the service that answered"""
        message = output if isinstance(output, ToolMessage) else None
        self._count_tool(
            run_id,
            message.artifact
            if message is not None and isinstance(message.artifact, str)
            else None,
            message is not None and message.status == "error",
        )

    @override
    async def on_tool_error(
        self, error: BaseException, *, run_id: UUID, **kwargs: object
    ) -> None:
        """Counts the call as failed and remembers its error"""
        self._count_tool(run_id, None, True)
        self._tool_errors.append(error)

    @override
    async def on_chain_error(
        self, error: BaseException, *, run_id: UUID, **kwargs: object
    ) -> None:
        """Keeps the error, which ends as the one the whole run failed with"""
        self._error = error

    def model_usage(self) -> list[ModelUsage]:
        """The run's tokens by agent, provider and model"""
        agents = dict.fromkeys(agent for agent, _ in self._tokens)
        return [
            ModelUsage(
                agent=agent,
                provider=usage.provider,
                model=usage.model,
                input_tokens=usage.input_tokens or 0,
                output_tokens=usage.output_tokens or 0,
                reasoning_tokens=usage.reasoning_tokens or 0,
                cached_input_tokens=usage.cached_input_tokens or 0,
                cache_write_input_tokens=usage.cache_write_input_tokens or 0,
            )
            for agent in agents
            for usage in aggregate_token_usage(
                [usage for owner, usage in self._tokens if owner == agent]
            )
        ]

    def tool_usage(self) -> list[ToolUsage]:
        """The run's tool calls by agent, tool and provider"""
        return list(self._tool_usage.values())

    def error_code(self) -> RunErrorCode:
        """Why the run failed, from the error that ended it"""
        error = self._error
        if isinstance(error, GraphRecursionError):
            return RunErrorCode.recursion_limit
        if error is not None and any(
            isinstance(cause, TimeoutError | httpx.TimeoutException)
            for cause in (error, error.__cause__)
        ):
            return RunErrorCode.timeout
        if any(error is tool_error for tool_error in self._tool_errors):
            return RunErrorCode.tool_error
        return RunErrorCode.model_error
