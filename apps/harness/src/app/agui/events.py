import asyncio
import time
from collections.abc import AsyncIterator, Callable, Collection
from contextlib import aclosing
from datetime import UTC, datetime
from uuid import uuid4

import anyio
import structlog
from ag_ui.core import (
    BaseEvent,
    CustomEvent,
    EventType,
    RunAgentInput,
    RunErrorEvent,
    RunFinishedCancelledOutcome,
    RunFinishedEvent,
    RunStartedEvent,
    SubagentStartedEvent,
    TextMessageContentEvent,
    TextMessageEndEvent,
    TextMessageStartEvent,
)
from ag_ui_langgraph import LangGraphAgent
from anthropic import AnthropicError
from langchain_core.messages import AIMessage
from langgraph.graph.state import CompiledStateGraph
from openai import OpenAIError
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from src.app.agui.served_agent import ServedAgent
from src.app.agui.usage import RunUsage
from src.infrastructure.persistence import (
    RunEnd,
    RunErrorCode,
    RunLost,
    RunStatus,
    StaleRun,
    UnreadableRun,
    finish_run,
    record_event,
)


def _innermost(error: BaseException) -> BaseException:
    """The first error inside the exception groups wrapping error"""
    if isinstance(error, BaseExceptionGroup):
        return _innermost(error.exceptions[0])
    return error


async def _resumed(
    graph: CompiledStateGraph, turn: RunAgentInput
) -> RunAgentInput | AIMessage:
    """The checkpoint's last AI message when it finished the turn, else the turn, after
    the latest step's node when the checkpoint holds the turn's user message"""
    history = [
        state
        async for state in graph.aget_state_history(
            {"configurable": {"thread_id": turn.thread_id}}, limit=2
        )
    ]
    messages = history[0].values.get("messages", []) if history else []
    if turn.run_id not in {message.id for message in messages}:
        return turn
    if not history[0].next:
        return next(
            (
                message
                for message in reversed(messages)
                if isinstance(message, AIMessage)
            ),
            AIMessage(""),
        )
    if len(history) < 2 or not history[1].next:
        return turn
    return turn.model_copy(update={"forwarded_props": {"nodeName": history[1].next[0]}})


async def _agent_events(
    served: ServedAgent, turn: RunAgentInput, usage: RunUsage, resumed: bool
) -> AsyncIterator[BaseEvent]:
    """The turn's AG-UI events after RUN_STARTED, without thread snapshots, ending with
    one RUN_FINISHED or RUN_ERROR; a resumed run opens with run_resumed"""
    finished = False
    if resumed:
        yield CustomEvent(name="run_resumed", value=None)
    try:
        async with served.open_graph() as graph:
            goes_on = await _resumed(graph, turn) if resumed else turn
            if isinstance(goes_on, AIMessage):
                message_id = goes_on.id or str(uuid4())
                if goes_on.text:
                    yield TextMessageStartEvent(message_id=message_id, role="assistant")
                    yield TextMessageContentEvent(
                        message_id=message_id, delta=goes_on.text
                    )
                    yield TextMessageEndEvent(message_id=message_id)
                finished = True
                yield RunFinishedEvent(thread_id=turn.thread_id, run_id=turn.run_id)
                return
            agent = LangGraphAgent(
                name=served.name,
                graph=graph,
                config={**graph.config, "callbacks": [usage]},
                emit_raw_events=False,
                subagent_visibility="attributed",
            )
            async with aclosing(agent.run(goes_on)) as events:
                async for event in events:
                    if event.type in (
                        EventType.RUN_STARTED,
                        EventType.MESSAGES_SNAPSHOT,
                        EventType.STATE_SNAPSHOT,
                    ):
                        continue
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
            yield RunErrorEvent(
                message=str(error) or type(error).__name__,
                code=RunErrorCode.model_error
                if isinstance(error, OpenAIError | AnthropicError)
                else RunErrorCode.tool_error,
            )


class _RunTally:
    def __init__(self, agent: str, provider: str, started_at: datetime) -> None:
        """Starts tallying what one run of agent, on provider, started at started_at,
        streams and uses"""
        self.agent = agent
        self.usage = RunUsage(agent, provider)
        self._started_at = started_at
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

    def end(self, event: RunFinishedEvent | RunErrorEvent) -> RunEnd:
        """How the run ends, as its stamped terminal event says"""
        failed = isinstance(event, RunErrorEvent)
        status = (
            RunStatus.error
            if failed
            else RunStatus.cancelled
            if isinstance(event.outcome, RunFinishedCancelledOutcome)
            else RunStatus.finished
        )
        return RunEnd(
            status=status,
            error_code=event.code if failed else None,
            error=event.message if failed else None,
            event=event,
            first_token_at=self._first_token_at,
            finished_at=datetime.now(UTC),
            answer=next(reversed(self._texts.values()), "")
            if status == RunStatus.finished
            else "",
            model_usage=self.usage.model_usage(),
            tool_usage=self.usage.tool_usage(),
        )


async def _end(
    connection: AsyncConnection,
    owner: str,
    run_id: str,
    tally: _RunTally,
    event: RunFinishedEvent | RunErrorEvent,
) -> None:
    """Ends the run with event, stamped, while owner holds it, and logs whether it
    ended or another process has it"""
    agent, stamped = tally.stamped(event)
    end = tally.end(stamped)
    if await finish_run(connection, run_id, owner, agent, end):
        structlog.stdlib.get_logger().info(
            "run ended",
            agent=agent,
            run_id=run_id,
            status=end.status,
            error_code=end.error_code,
        )
    else:
        structlog.stdlib.get_logger().warning(
            "run lost to another process", agent=agent, run_id=run_id
        )


def _cancelled(turn: RunAgentInput) -> RunFinishedEvent:
    """The RUN_FINISHED that ends the turn's run as cancelled"""
    return RunFinishedEvent(
        thread_id=turn.thread_id,
        run_id=turn.run_id,
        outcome=RunFinishedCancelledOutcome(),
    )


def run_started(agent: str, turn: RunAgentInput) -> RunStartedEvent:
    """The RUN_STARTED of the turn's run on the agent, stamped with its time and agent"""
    return RunStartedEvent(
        thread_id=turn.thread_id,
        run_id=turn.run_id,
        timestamp=round(time.time() * 1000),
        metadata={"agent": agent},
    )


def stale_end(
    run: StaleRun | UnreadableRun, max_attempts: int
) -> RunFinishedEvent | RunErrorEvent | None:
    """The event that ends a taken-over run at once, or None when it resumes"""
    if isinstance(run, UnreadableRun):
        return RunErrorEvent(message=run.error, code=RunErrorCode.storage_error)
    if run.cancel_requested:
        return _cancelled(run.input)
    if not run.resumable:
        return RunErrorEvent(
            message="The assistant stopped responding.", code=RunErrorCode.timeout
        )
    if run.attempts > max_attempts:
        return RunErrorEvent(
            message="The assistant kept stopping on this message.",
            code=RunErrorCode.timeout,
        )
    return None


async def end_run(
    engine: AsyncEngine,
    owner: str,
    served: ServedAgent,
    run: StaleRun | UnreadableRun,
    event: RunFinishedEvent | RunErrorEvent,
) -> None:
    """Ends a run no process works on with event, while owner holds it"""
    async with engine.connect() as connection:
        await _end(
            connection,
            owner,
            run.id,
            _RunTally(served.name, served.model_provider, run.created_at),
            event,
        )


async def record_run(
    engine: AsyncEngine,
    owner: str,
    served: ServedAgent,
    turn: RunAgentInput,
    started_at: datetime,
    resumed: bool,
    cancels: Collection[str],
    ended: Callable[[], None],
) -> None:
    """Runs the turn on the agent, recording each event while owner holds the run and
    calling ended once it ends; a cancel of a run in cancels ends it as cancelled"""
    tally = _RunTally(served.name, served.model_provider, started_at)
    finished = False
    connection = await engine.connect()
    try:
        async with aclosing(
            _agent_events(served, turn, tally.usage, resumed)
        ) as events:
            async for event in events:
                if isinstance(event, RunFinishedEvent | RunErrorEvent):
                    await _end(connection, owner, turn.run_id, tally, event)
                    finished = True
                    ended()
                else:
                    agent, stamped = tally.stamped(event)
                    await record_event(connection, turn.run_id, owner, agent, stamped)
    except RunLost:
        structlog.stdlib.get_logger().warning(
            "run lost to another process", agent=served.name, run_id=turn.run_id
        )
    except asyncio.CancelledError:
        if finished or turn.run_id not in cancels:
            raise
        with anyio.CancelScope(shield=True):
            async with engine.connect() as ending:
                await _end(ending, owner, turn.run_id, tally, _cancelled(turn))
    except Exception as error:
        if finished:
            raise
        async with engine.connect() as ending:
            await _end(
                ending,
                owner,
                turn.run_id,
                tally,
                RunErrorEvent(
                    message=str(error.orig if isinstance(error, DBAPIError) else error)
                    or type(error).__name__,
                    code=RunErrorCode.storage_error,
                ),
            )
    finally:
        with anyio.CancelScope(shield=True):
            await connection.close()
