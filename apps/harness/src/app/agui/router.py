from collections.abc import AsyncIterator
from contextlib import aclosing
from datetime import datetime
from typing import Annotated

from ag_ui.core import RunAgentInput, TextInputContent, UserMessage
from fastapi import (
    APIRouter,
    Depends,
    Header,
    HTTPException,
    Query,
    Request,
    Response,
    status,
)
from fastapi.sse import EventSourceResponse, ServerSentEvent
from pydantic import BaseModel, ConfigDict
from pydantic.alias_generators import to_camel
from sqlalchemy.ext.asyncio import AsyncEngine

from src.app.agui.runner import Runner, RunnerFull
from src.app.agui.served_agent import ServedAgent
from src.app.auth import Claims, verified_claims
from src.infrastructure.persistence import (
    RunExists,
    RunView,
    ThreadBusy,
    ThreadMessage,
    find_run,
    request_cancel,
    thread_id,
    thread_messages,
)

router = APIRouter()


class RunConflict(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        use_attribute_docstrings=True,
        alias_generator=to_camel,
        populate_by_name=True,
    )

    message: str
    """Why the run was refused"""

    run_id: str | None
    """The run already running on the thread, or none when the run id is taken on
    another thread"""


def served_agent(agent: str, request: Request) -> ServedAgent:
    """The agent the path names; 404 when the harness serves none by that name"""
    served = request.app.state.agents.get(agent)
    if served is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"No agent called {agent}")
    return served


def database(request: Request) -> AsyncEngine:
    """The engine on the harness's tables"""
    return request.app.state.engine


def runner(request: Request) -> Runner:
    """What works on agents' runs in this process"""
    return request.app.state.runner


def user_message(run: RunAgentInput) -> UserMessage:
    """The client's last user message, the only one a run takes; 422 when it sent
    none"""
    message = next(
        (
            message
            for message in reversed(run.messages)
            if isinstance(message, UserMessage)
        ),
        None,
    )
    if message is None:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT, "A run needs a user message"
        )
    return message


def user_text(message: UserMessage) -> str:
    """The text of a user message, without its other content such as images"""
    if isinstance(message.content, str):
        return message.content
    return "".join(
        part.text for part in message.content if isinstance(part, TextInputContent)
    )


async def started_run(
    run: RunAgentInput,
    claims: Annotated[Claims, Depends(verified_claims)],
    served: Annotated[ServedAgent, Depends(served_agent)],
    message: Annotated[UserMessage, Depends(user_message)],
    engine: Annotated[AsyncEngine, Depends(database)],
    runs: Annotated[Runner, Depends(runner)],
) -> str:
    """Starts the client's last user message as a run on the caller's thread, or finds
    the thread's run with that id; 409 for a taken id or busy thread, 503 when full"""
    thread = await thread_id(engine, served.name, claims.client_id, claims.user_id)
    if await find_run(
        engine, served.name, claims.client_id, claims.user_id, run.run_id
    ):
        return run.run_id
    turn = RunAgentInput(
        thread_id=str(thread),
        run_id=run.run_id,
        messages=[UserMessage(id=run.run_id, content=message.content)],
    )
    try:
        await runs.start(served, thread, turn, user_text(message))
    except RunnerFull as error:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "The harness is busy, try again later",
            headers={"Retry-After": str(error.retry_after_s)},
        ) from error
    except RunExists as error:
        if error.thread_id == thread:
            return run.run_id
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            RunConflict(message="The run id is taken", run_id=None).model_dump(
                by_alias=True
            ),
        ) from error
    except ThreadBusy as error:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            RunConflict(
                message="Another run is running on this thread",
                run_id=error.running_run_id,
            ).model_dump(by_alias=True),
        ) from error
    return run.run_id


async def callers_run(
    run_id: str,
    claims: Annotated[Claims, Depends(verified_claims)],
    served: Annotated[ServedAgent, Depends(served_agent)],
    engine: Annotated[AsyncEngine, Depends(database)],
) -> RunView:
    """One of the caller's runs with the agent; 404 when it has none by that id"""
    found = await find_run(
        engine, served.name, claims.client_id, claims.user_id, run_id
    )
    if found is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"No run {run_id}")
    return found


@router.post("/{agent}", response_class=EventSourceResponse)
async def run_agent(
    run_id: Annotated[str, Depends(started_run)],
    runs: Annotated[Runner, Depends(runner)],
    last_event_id: Annotated[int, Header()] = 0,
) -> AsyncIterator[ServerSentEvent]:
    """Streams the run's AG-UI events, after Last-Event-ID when sent, until it ends"""
    async with aclosing(runs.events(run_id, last_event_id)) as events:
        async for event in events:
            yield event


@router.get("/{agent}/runs/{run_id}")
async def run_status(run: Annotated[RunView, Depends(callers_run)]) -> RunView:
    """Where one of the caller's runs with the agent is"""
    return run


@router.get("/{agent}/runs/{run_id}/events", response_class=EventSourceResponse)
async def run_events(
    run: Annotated[RunView, Depends(callers_run)],
    runs: Annotated[Runner, Depends(runner)],
    last_event_id: Annotated[int, Header()] = 0,
) -> AsyncIterator[ServerSentEvent]:
    """Streams one of the caller's runs' AG-UI events, after Last-Event-ID when sent,
    until it ends"""
    async with aclosing(runs.events(run.id, last_event_id)) as events:
        async for event in events:
            yield event


@router.post(
    "/{agent}/runs/{run_id}/cancel",
    status_code=status.HTTP_202_ACCEPTED,
    response_class=Response,
)
async def cancel_run(
    run: Annotated[RunView, Depends(callers_run)],
    engine: Annotated[AsyncEngine, Depends(database)],
) -> None:
    """Asks the process working on one of the caller's runs to cancel it; 409 when it
    already ended"""
    if not await request_cancel(engine, run.id):
        raise HTTPException(status.HTTP_409_CONFLICT, f"Run {run.id} already ended")


@router.get("/{agent}/threads/messages")
async def thread_transcript(
    claims: Annotated[Claims, Depends(verified_claims)],
    served: Annotated[ServedAgent, Depends(served_agent)],
    engine: Annotated[AsyncEngine, Depends(database)],
    before: datetime | None = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
) -> list[ThreadMessage]:
    """The latest messages of the caller's thread with the agent, oldest first,
    written before before when given"""
    return await thread_messages(
        engine, served.name, claims.client_id, claims.user_id, before, limit
    )
