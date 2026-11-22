from datetime import datetime
from uuid import UUID, uuid4

from ag_ui.core import BaseEvent, RunAgentInput, RunErrorEvent, RunFinishedEvent
from pydantic import BaseModel, ConfigDict
from pydantic.alias_generators import to_camel
from sqlalchemy import insert, select, update
from sqlalchemy.dialects.postgresql import insert as upsert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from src.infrastructure.persistence.tables import (
    MessageRole,
    RunErrorCode,
    RunStatus,
    messages,
    run_events,
    run_model_usage,
    run_tool_usage,
    runs,
    threads,
)


class RunExists(Exception):
    pass


class ThreadBusy(Exception):
    def __init__(self, running_run_id: str | None) -> None:
        """Names the run already running on the thread"""
        super().__init__(f"run {running_run_id} is already running on this thread")
        self.running_run_id = running_run_id


class ModelUsage(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        use_attribute_docstrings=True,
        alias_generator=to_camel,
        populate_by_name=True,
    )

    agent: str
    """The agent that called the model, such as sothoth or a subagent"""

    provider: str
    """The provider the agent's model family runs on: openai, anthropic or gateway"""

    model: str | None
    """The model, such as gpt-6-luna, when its chat model names it"""

    input_tokens: int
    """Every prompt token charged, cached ones included"""

    output_tokens: int
    """Every generated token, reasoning included"""

    reasoning_tokens: int
    """Output tokens spent on reasoning"""

    cached_input_tokens: int
    """Input tokens read from the provider's cache"""

    cache_write_input_tokens: int
    """Input tokens written to the provider's cache"""


class ToolUsage(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        use_attribute_docstrings=True,
        alias_generator=to_camel,
        populate_by_name=True,
    )

    agent: str
    """The agent that called the tool"""

    tool: str
    """The tool's name, such as web_search"""

    provider: str | None
    """The service that answered, for a tool that reports one, such as tavily"""

    calls: int
    """Times the agent called the tool"""

    failures: int
    """Calls that raised or returned an error to the model"""

    duration_ms: int
    """Time spent in all the calls, in milliseconds"""


class RunEnd(BaseModel):
    model_config = ConfigDict(
        extra="forbid", frozen=True, use_attribute_docstrings=True
    )

    status: RunStatus
    """How the run ended"""

    error_code: RunErrorCode | None
    """Why the run failed, when it did"""

    error: str | None
    """The error the run failed with, for a person to read"""

    event: RunFinishedEvent | RunErrorEvent | None
    """The terminal event the run sent, None when the client left before one"""

    first_token_at: datetime | None
    """When the run streamed its first text"""

    finished_at: datetime
    """When the run ended"""

    answer: str
    """The agent's final text, empty when it gave none"""

    model_usage: list[ModelUsage]
    """Tokens each agent spent on each model"""

    tool_usage: list[ToolUsage]
    """Each agent's calls to each tool"""


class RunView(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        use_attribute_docstrings=True,
        alias_generator=to_camel,
        populate_by_name=True,
    )

    id: str
    """The run's id, the client's runId"""

    status: RunStatus
    """Where the run is"""

    error_code: RunErrorCode | None
    """Why the run failed, when it did"""

    error: str | None
    """The error the run failed with, for a person to read"""

    created_at: datetime
    """When the run started"""

    first_token_at: datetime | None
    """When the run streamed its first text"""

    finished_at: datetime | None
    """When the run ended"""


class ThreadMessage(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        use_attribute_docstrings=True,
        alias_generator=to_camel,
        populate_by_name=True,
    )

    id: UUID
    """The message's id"""

    run_id: str
    """The run the message belongs to"""

    role: MessageRole
    """Who wrote it"""

    content: str
    """Its text"""

    created_at: datetime
    """When it was written"""


async def thread_id(
    engine: AsyncEngine, agent: str, client_id: str, user_id: UUID
) -> UUID:
    """The id of the user's thread with the agent through the client, created on
    first use"""
    async with engine.begin() as connection:
        created = await connection.scalar(
            upsert(threads)
            .values(agent=agent, client_id=client_id, user_id=user_id)
            .on_conflict_do_nothing()
            .returning(threads.c.id)
        )
        return created or await connection.scalar(
            select(threads.c.id).where(
                threads.c.agent == agent,
                threads.c.client_id == client_id,
                threads.c.user_id == user_id,
            )
        )


async def start_run(
    engine: AsyncEngine, thread: UUID, turn: RunAgentInput, user_text: str
) -> None:
    """Records the turn as a running run with the user's text, raising RunExists for
    a known run id and ThreadBusy while another run is running on the thread"""
    try:
        async with engine.begin() as connection:
            await connection.execute(
                insert(runs).values(
                    id=turn.run_id,
                    thread_id=thread,
                    status=RunStatus.running,
                    input=turn.model_dump(mode="json", by_alias=True),
                )
            )
            await connection.execute(
                insert(messages).values(
                    id=uuid4(),
                    thread_id=thread,
                    run_id=turn.run_id,
                    role=MessageRole.user,
                    content=user_text,
                )
            )
    except IntegrityError as error:
        constraint = error.orig.diag.constraint_name
        if constraint == "runs_pkey":
            raise RunExists(turn.run_id) from error
        if constraint != "ix_runs_running_thread_id":
            raise
        async with engine.connect() as connection:
            raise ThreadBusy(
                await connection.scalar(
                    select(runs.c.id).where(
                        runs.c.thread_id == thread,
                        runs.c.status == RunStatus.running,
                    )
                )
            ) from error


async def _insert_event(
    connection: AsyncConnection, run_id: str, agent: str, event: BaseEvent
) -> int:
    """Appends the event to the run's events and returns its id"""
    recorded = await connection.execute(
        insert(run_events)
        .values(
            run_id=run_id,
            agent=agent,
            type=event.type.value,
            event=event.model_dump(mode="json", by_alias=True),
        )
        .returning(run_events.c.id)
    )
    return recorded.scalar_one()


async def record_event(
    connection: AsyncConnection, run_id: str, agent: str, event: BaseEvent
) -> int:
    """Appends the event to the run's events in a transaction of its own and returns
    its id"""
    async with connection.begin():
        return await _insert_event(connection, run_id, agent, event)


async def finish_run(
    connection: AsyncConnection, run_id: str, agent: str, end: RunEnd
) -> int | None:
    """Ends the running run in one transaction with its terminal event, answer and
    usage, and returns the event's id; does nothing to a run that already ended"""
    async with connection.begin():
        thread = await connection.scalar(
            update(runs)
            .where(runs.c.id == run_id, runs.c.status == RunStatus.running)
            .values(
                status=end.status,
                error_code=end.error_code,
                error=end.error,
                first_token_at=end.first_token_at,
                finished_at=end.finished_at,
            )
            .returning(runs.c.thread_id)
        )
        if thread is None:
            return None
        if end.answer:
            await connection.execute(
                insert(messages).values(
                    id=uuid4(),
                    thread_id=thread,
                    run_id=run_id,
                    role=MessageRole.assistant,
                    content=end.answer,
                )
            )
        if end.model_usage:
            await connection.execute(
                insert(run_model_usage),
                [{"run_id": run_id, **usage.model_dump()} for usage in end.model_usage],
            )
        if end.tool_usage:
            await connection.execute(
                insert(run_tool_usage),
                [{"run_id": run_id, **usage.model_dump()} for usage in end.tool_usage],
            )
        return (
            None
            if end.event is None
            else await _insert_event(connection, run_id, agent, end.event)
        )


async def find_run(
    engine: AsyncEngine, agent: str, client_id: str, user_id: UUID, run_id: str
) -> RunView | None:
    """The run, when it is on the user's thread with the agent through the client"""
    async with engine.connect() as connection:
        row = (
            await connection.execute(
                select(
                    runs.c.id,
                    runs.c.status,
                    runs.c.error_code,
                    runs.c.error,
                    runs.c.created_at,
                    runs.c.first_token_at,
                    runs.c.finished_at,
                )
                .join(threads, threads.c.id == runs.c.thread_id)
                .where(
                    runs.c.id == run_id,
                    threads.c.agent == agent,
                    threads.c.client_id == client_id,
                    threads.c.user_id == user_id,
                )
            )
        ).first()
    return None if row is None else RunView.model_validate(row._asdict())


async def thread_messages(
    engine: AsyncEngine,
    agent: str,
    client_id: str,
    user_id: UUID,
    before: datetime | None,
    limit: int,
) -> list[ThreadMessage]:
    """The latest limit messages of the user's thread with the agent through the
    client, written before before when given, oldest first"""
    query = (
        select(
            messages.c.id,
            messages.c.run_id,
            messages.c.role,
            messages.c.content,
            messages.c.created_at,
        )
        .join(threads, threads.c.id == messages.c.thread_id)
        .where(
            threads.c.agent == agent,
            threads.c.client_id == client_id,
            threads.c.user_id == user_id,
        )
        .order_by(messages.c.created_at.desc())
        .limit(limit)
    )
    if before is not None:
        query = query.where(messages.c.created_at < before)
    async with engine.connect() as connection:
        rows = (await connection.execute(query)).all()
    return [ThreadMessage.model_validate(row._asdict()) for row in reversed(rows)]
