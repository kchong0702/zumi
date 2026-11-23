from collections.abc import Collection
from datetime import datetime, timedelta
from uuid import UUID, uuid4

from ag_ui.core import (
    BaseEvent,
    RunAgentInput,
    RunErrorEvent,
    RunFinishedEvent,
    RunStartedEvent,
)
from pydantic import BaseModel, ConfigDict, ValidationError
from pydantic.alias_generators import to_camel
from sqlalchemy import Row, Text, and_, cast, func, insert, or_, select, update
from sqlalchemy.dialects.postgresql import insert as upsert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from src.infrastructure.persistence.tables import (
    MessageRole,
    RunChannel,
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
    def __init__(self, thread_id: UUID) -> None:
        """Names the thread the run with the same id is on"""
        super().__init__(f"the run already exists on thread {thread_id}")
        self.thread_id = thread_id


class RunLost(Exception):
    def __init__(self, run_id: str) -> None:
        """Names the run this process no longer owns, or that already ended"""
        super().__init__(f"run {run_id} is no longer this process's to run")
        self.run_id = run_id


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

    event: RunFinishedEvent | RunErrorEvent
    """The terminal event the run sent"""

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


class RecordedEvent(BaseModel):
    model_config = ConfigDict(
        extra="forbid", frozen=True, use_attribute_docstrings=True
    )

    id: int
    """The event's row id, which orders a run's events"""

    data: str
    """The AG-UI event as JSON"""


class StaleRun(BaseModel):
    model_config = ConfigDict(
        extra="forbid", frozen=True, use_attribute_docstrings=True
    )

    id: str
    """The run's id"""

    agent: str
    """The agent the run's thread is with"""

    input: RunAgentInput
    """The turn the run runs"""

    created_at: datetime
    """When the run started"""

    resumable: bool
    """Whether the run started recently enough to resume, by the database's clock"""

    cancel_requested: bool
    """Whether someone asked to cancel the run"""

    attempts: int
    """Times a process took the run over, this time included"""


class UnreadableRun(BaseModel):
    model_config = ConfigDict(
        extra="forbid", frozen=True, use_attribute_docstrings=True
    )

    id: str
    """The run's id"""

    agent: str
    """The agent the run's thread is with"""

    created_at: datetime
    """When the run started"""

    error: str
    """Why the run's stored turn could not be read"""


class Heartbeat(BaseModel):
    model_config = ConfigDict(
        extra="forbid", frozen=True, use_attribute_docstrings=True
    )

    lost: frozenset[str]
    """The runs another process now owns"""

    cancel_requested: frozenset[str]
    """The owner's running runs someone asked to cancel"""


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


async def start_run(
    engine: AsyncEngine,
    thread: UUID,
    turn: RunAgentInput,
    user_text: str,
    owner: str,
    agent: str,
    started: RunStartedEvent,
) -> None:
    """Records the turn as a running run of owner with the user's text and its first
    event, started; raises RunExists for a known run id, ThreadBusy for a busy thread"""
    try:
        async with engine.begin() as connection:
            await connection.execute(
                insert(runs).values(
                    id=turn.run_id,
                    thread_id=thread,
                    status=RunStatus.running,
                    input=turn.model_dump(mode="json", by_alias=True),
                    owner=owner,
                    heartbeat_at=func.now(),
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
            await _insert_event(connection, turn.run_id, agent, started)
    except IntegrityError as error:
        async with engine.connect() as connection:
            existing = await connection.scalar(
                select(runs.c.thread_id).where(runs.c.id == turn.run_id)
            )
            if existing is not None:
                raise RunExists(existing) from error
            if error.orig.diag.constraint_name != "ix_runs_running_thread_id":
                raise
            raise ThreadBusy(
                await connection.scalar(
                    select(runs.c.id).where(
                        runs.c.thread_id == thread,
                        runs.c.status == RunStatus.running,
                    )
                )
            ) from error


async def _notify(
    connection: AsyncConnection, channel: RunChannel, run_id: str
) -> None:
    """Tells every process listening on the schema's channel about the run, once the
    transaction commits"""
    await connection.execute(
        select(
            func.pg_notify(func.concat(func.current_schema(), f".{channel}"), run_id)
        )
    )


async def record_event(
    connection: AsyncConnection, run_id: str, owner: str, agent: str, event: BaseEvent
) -> int:
    """Appends the event to the run's events in a transaction of its own and returns
    its id; raises RunLost unless owner holds the run and it is running"""
    async with connection.begin():
        held = await connection.scalar(
            select(runs.c.id)
            .where(
                runs.c.id == run_id,
                runs.c.owner == owner,
                runs.c.status == RunStatus.running,
            )
            .with_for_update(read=True)
        )
        if held is None:
            raise RunLost(run_id)
        event_id = await _insert_event(connection, run_id, agent, event)
        await _notify(connection, RunChannel.events, run_id)
        return event_id


async def finish_run(
    connection: AsyncConnection, run_id: str, owner: str, agent: str, end: RunEnd
) -> bool:
    """Ends the run in one transaction with its terminal event, answer and usage, and
    says whether it did: only while owner holds the run and it is running"""
    async with connection.begin():
        thread = await connection.scalar(
            update(runs)
            .where(
                runs.c.id == run_id,
                runs.c.owner == owner,
                runs.c.status == RunStatus.running,
            )
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
            return False
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
        await _insert_event(connection, run_id, agent, end.event)
        await _notify(connection, RunChannel.events, run_id)
        return True


async def events_after(
    engine: AsyncEngine, run_id: str, after: int
) -> tuple[bool, list[RecordedEvent]]:
    """Whether the run is still running, and its events recorded after the event id
    after, in order; read in that order, so an ended run's events are all there"""
    async with engine.connect() as connection:
        running = (
            await connection.scalar(select(runs.c.status).where(runs.c.id == run_id))
            == RunStatus.running
        )
        rows = (
            await connection.execute(
                select(run_events.c.id, cast(run_events.c.event, Text).label("data"))
                .where(run_events.c.run_id == run_id, run_events.c.id > after)
                .order_by(run_events.c.id)
            )
        ).all()
    return running, [RecordedEvent.model_validate(row._asdict()) for row in rows]


async def request_cancel(engine: AsyncEngine, run_id: str) -> bool:
    """Marks the running run as asked to be cancelled and notifies its owner; whether
    it was still running"""
    async with engine.begin() as connection:
        requested = await connection.scalar(
            update(runs)
            .where(runs.c.id == run_id, runs.c.status == RunStatus.running)
            .values(cancel_requested_at=func.now())
            .returning(runs.c.id)
        )
        if requested is None:
            return False
        await _notify(connection, RunChannel.cancels, run_id)
        return True


async def heartbeat(
    engine: AsyncEngine, owner: str, run_ids: Collection[str]
) -> Heartbeat:
    """Marks owner's running runs among run_ids alive, and says which of run_ids
    another process owns and which of them someone asked to cancel"""
    async with engine.begin() as connection:
        await connection.execute(
            update(runs)
            .where(
                runs.c.id.in_(run_ids),
                runs.c.owner == owner,
                runs.c.status == RunStatus.running,
            )
            .values(heartbeat_at=func.now())
        )
        rows = (
            await connection.execute(
                select(
                    runs.c.id,
                    runs.c.owner.is_distinct_from(owner).label("lost"),
                    and_(
                        runs.c.status == RunStatus.running,
                        runs.c.cancel_requested_at.is_not(None),
                    ).label("cancel_requested"),
                ).where(runs.c.id.in_(run_ids))
            )
        ).all()
    return Heartbeat(
        lost=frozenset(row.id for row in rows if row.lost),
        cancel_requested=frozenset(
            row.id for row in rows if row.cancel_requested and not row.lost
        ),
    )


def _claimed(row: Row) -> StaleRun | UnreadableRun:
    """The claimed run in row, or the run as unreadable when its stored turn isn't one"""
    try:
        return StaleRun.model_validate(row._asdict())
    except ValidationError as error:
        return UnreadableRun(
            id=row.id, agent=row.agent, created_at=row.created_at, error=str(error)
        )


async def claim_stale(
    engine: AsyncEngine,
    owner: str,
    agents: Collection[str],
    dead_after_s: float,
    resume_within_s: float,
    limit: int,
) -> list[StaleRun | UnreadableRun]:
    """Makes owner the owner of up to limit running runs of agents not marked alive
    for dead_after_s, locking each so only one of two racing processes gets it"""
    stale = (
        select(runs.c.id)
        .join(threads, threads.c.id == runs.c.thread_id)
        .where(
            threads.c.agent.in_(agents),
            runs.c.status == RunStatus.running,
            or_(
                runs.c.heartbeat_at.is_(None),
                runs.c.heartbeat_at < func.now() - timedelta(seconds=dead_after_s),
            ),
        )
        .limit(limit)
        .with_for_update(of=runs, skip_locked=True)
    )
    async with engine.begin() as connection:
        rows = await connection.execute(
            update(runs)
            .where(runs.c.thread_id == threads.c.id, runs.c.id.in_(stale))
            .values(owner=owner, heartbeat_at=func.now(), attempts=runs.c.attempts + 1)
            .returning(
                runs.c.id,
                threads.c.agent,
                runs.c.input,
                runs.c.created_at,
                (
                    runs.c.created_at >= func.now() - timedelta(seconds=resume_within_s)
                ).label("resumable"),
                runs.c.cancel_requested_at.is_not(None).label("cancel_requested"),
                runs.c.attempts,
            )
        )
        return [_claimed(row) for row in rows.all()]


async def release(engine: AsyncEngine, owner: str) -> None:
    """Clears the heartbeats of owner's running runs, so the next sweep of any process
    takes them over"""
    async with engine.begin() as connection:
        await connection.execute(
            update(runs)
            .where(runs.c.owner == owner, runs.c.status == RunStatus.running)
            .values(heartbeat_at=None)
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
