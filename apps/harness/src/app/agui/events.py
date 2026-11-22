import time
from collections.abc import AsyncIterator
from contextlib import aclosing
from datetime import UTC, datetime

import anyio
import structlog
from ag_ui.core import (
    BaseEvent,
    EventType,
    RunAgentInput,
    RunErrorEvent,
    RunFinishedEvent,
    RunStartedEvent,
    SubagentStartedEvent,
    TextMessageContentEvent,
)
from ag_ui_langgraph import LangGraphAgent
from anthropic import AnthropicError
from fastapi.sse import ServerSentEvent
from openai import OpenAIError
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from src.app.agui.served_agent import ServedAgent
from src.app.agui.usage import RunUsage
from src.infrastructure.persistence import (
    RunEnd,
    RunErrorCode,
    RunStatus,
    finish_run,
    record_event,
)


def _innermost(error: BaseException) -> BaseException:
    """The first error inside the exception groups wrapping error"""
    if isinstance(error, BaseExceptionGroup):
        return _innermost(error.exceptions[0])
    return error


async def _agent_events(
    served: ServedAgent, turn: RunAgentInput, usage: RunUsage
) -> AsyncIterator[BaseEvent]:
    """The turn's AG-UI events on the agent's thread, without snapshots of the whole
    thread, always between one RUN_STARTED and one RUN_FINISHED or RUN_ERROR"""
    started = finished = False
    try:
        async with (
            served.open_graph() as graph,
            aclosing(
                LangGraphAgent(
                    name=served.name,
                    graph=graph,
                    config={**graph.config, "callbacks": [usage]},
                    emit_raw_events=False,
                    subagent_visibility="attributed",
                ).run(turn)
            ) as events,
        ):
            async for event in events:
                if event.type in (
                    EventType.MESSAGES_SNAPSHOT,
                    EventType.STATE_SNAPSHOT,
                ):
                    continue
                started = started or event.type == EventType.RUN_STARTED
                finished = finished or event.type in (
                    EventType.RUN_FINISHED,
                    EventType.RUN_ERROR,
                )
                yield event
    except* Exception as errors:
        error = _innermost(errors)
        if finished:
            structlog.stdlib.get_logger().error(
                "the run failed after its terminal event",
                agent=served.name,
                run_id=turn.run_id,
                exc_info=error,
            )
        else:
            if not started:
                yield RunStartedEvent(thread_id=turn.thread_id, run_id=turn.run_id)
            yield RunErrorEvent(
                message=str(error) or type(error).__name__,
                code=RunErrorCode.model_error
                if isinstance(error, OpenAIError | AnthropicError)
                else RunErrorCode.tool_error,
            )


class _RunTally:
    def __init__(self, agent: str, provider: str) -> None:
        """Starts tallying what one run of agent, on provider, streams and uses"""
        self.agent = agent
        self.usage = RunUsage(agent, provider)
        self._started_at = datetime.now(UTC)
        self._first_token_at: datetime | None = None
        self._subagents: dict[str, str] = {}
        self._texts: dict[str, str] = {}

    def _ms_since_start(self, at: datetime) -> int:
        """Milliseconds from the run's start to at"""
        return round((at - self._started_at).total_seconds() * 1000)

    def _agent(self, event: BaseEvent) -> str:
        """The agent that produced event: the subagent ag-ui-langgraph attributes it
        to, or the served agent"""
        if isinstance(event, SubagentStartedEvent):
            self._subagents[event.subagent_run_id] = event.name
        subagent_run_id = getattr(event, "subagent_run_id", None)
        if subagent_run_id is None:
            return self.agent
        return self._subagents.get(subagent_run_id, self.agent)

    def stamped(self, event: BaseEvent) -> tuple[str, BaseEvent]:
        """The agent that produced event, and event stamped with its time and agent;
        a terminal event also gets the usage by agent and tool, timings and a code"""
        agent = self._agent(event)
        if isinstance(event, TextMessageContentEvent):
            self._first_token_at = self._first_token_at or datetime.now(UTC)
            if agent == self.agent:
                self._texts[event.message_id] = (
                    self._texts.get(event.message_id, "") + event.delta
                )
        metadata = {**(event.metadata or {}), "agent": agent}
        update: dict[str, object] = {"timestamp": round(time.time() * 1000)}
        if isinstance(event, RunFinishedEvent | RunErrorEvent):
            metadata |= {
                "agentUsage": [
                    usage.model_dump(by_alias=True)
                    for usage in self.usage.model_usage()
                ],
                "toolUsage": [
                    usage.model_dump(by_alias=True) for usage in self.usage.tool_usage()
                ],
                "durationMs": self._ms_since_start(datetime.now(UTC)),
                "timeToFirstTokenMs": None
                if self._first_token_at is None
                else self._ms_since_start(self._first_token_at),
            }
            if isinstance(event, RunErrorEvent):
                update["code"] = (
                    RunErrorCode(event.code) if event.code else self.usage.error_code()
                )
        return agent, event.model_copy(update={**update, "metadata": metadata})

    def end(self, event: RunFinishedEvent | RunErrorEvent | None) -> RunEnd:
        """How the run ends: as its stamped terminal event says, or cancelled when the
        client left before one"""
        failed = isinstance(event, RunErrorEvent)
        return RunEnd(
            status=RunStatus.error
            if failed
            else RunStatus.cancelled
            if event is None
            else RunStatus.finished,
            error_code=event.code if failed else None,
            error=event.message if failed else None,
            event=event,
            first_token_at=self._first_token_at,
            finished_at=datetime.now(UTC),
            answer=next(reversed(self._texts.values()), "")
            if isinstance(event, RunFinishedEvent)
            else "",
            model_usage=self.usage.model_usage(),
            tool_usage=self.usage.tool_usage(),
        )


async def _finish(
    connection: AsyncConnection, run_id: str, agent: str, end: RunEnd
) -> int | None:
    """Ends the run as end says, logs how, and returns its terminal event's id"""
    event_id = await finish_run(connection, run_id, agent, end)
    structlog.stdlib.get_logger().info(
        "run ended",
        agent=agent,
        run_id=run_id,
        status=end.status,
        error_code=end.error_code,
    )
    return event_id


def _sse(event: BaseEvent, event_id: int | None) -> ServerSentEvent:
    """event as an SSE event whose id is the run_events row that holds it"""
    return ServerSentEvent(
        raw_data=event.model_dump_json(by_alias=True),
        id=None if event_id is None else str(event_id),
    )


async def run_events(
    engine: AsyncEngine, served: ServedAgent, turn: RunAgentInput
) -> AsyncIterator[ServerSentEvent]:
    """Runs the turn on the agent and streams its events as SSE, each recorded first;
    the run ends with its terminal event, or cancelled when the client leaves first"""
    tally = _RunTally(served.name, served.model_provider)
    ended = False
    connection = await engine.connect()
    try:
        try:
            async with aclosing(_agent_events(served, turn, tally.usage)) as events:
                async for event in events:
                    agent, stamped = tally.stamped(event)
                    if isinstance(stamped, RunFinishedEvent | RunErrorEvent):
                        event_id = await _finish(
                            connection, turn.run_id, agent, tally.end(stamped)
                        )
                        ended = True
                    else:
                        event_id = await record_event(
                            connection, turn.run_id, agent, stamped
                        )
                    yield _sse(stamped, event_id)
        except Exception as error:
            if ended:
                raise
            agent, stamped = tally.stamped(
                RunErrorEvent(
                    message=str(error.orig if isinstance(error, DBAPIError) else error)
                    or type(error).__name__,
                    code=RunErrorCode.storage_error,
                )
            )
            async with engine.connect() as ending:
                event_id = await _finish(ending, turn.run_id, agent, tally.end(stamped))
            ended = True
            yield _sse(stamped, event_id)
    finally:
        with anyio.CancelScope(shield=True):
            await connection.close()
            if not ended:
                async with engine.connect() as ending:
                    await _finish(ending, turn.run_id, served.name, tally.end(None))
