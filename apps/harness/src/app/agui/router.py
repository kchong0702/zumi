from collections.abc import AsyncIterator
from contextlib import aclosing
from datetime import datetime
from typing import Annotated

from ag_ui.core import RunAgentInput, TextInputContent, UserMessage
from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from fastapi.sse import EventSourceResponse, ServerSentEvent
from pydantic import BaseModel, ConfigDict
from pydantic.alias_generators import to_camel
from sqlalchemy.ext.asyncio import AsyncEngine

from src.app.agui.events import run_events
from src.app.agui.served_agent import ServedAgent
from src.app.auth import Claims, verified_claims
from src.infrastructure.persistence import (
    RunExists,
    RunView,
    ThreadBusy,
    ThreadMessage,
    find_run,
    start_run,
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
    """The run in the way: the one already running on the thread, or the run with
    the same id"""


def served_agent(agent: str, request: Request) -> ServedAgent:
    """The agent the path names; 404 when the harness serves none by that name"""
    served = request.app.state.agents.get(agent)
    if served is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"No agent called {agent}")
    return served


def database(request: Request) -> AsyncEngine:
    """The engine on the harness's tables"""
    return request.app.state.engine


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
) -> RunAgentInput:
    """Records the run on the caller's thread and returns the turn it runs, the client's
    last user message alone under the run's id; 409 when the id is taken or thread busy"""
    thread = await thread_id(engine, served.name, claims.client_id, claims.user_id)
    turn = RunAgentInput(
        thread_id=str(thread),
        run_id=run.run_id,
        messages=[UserMessage(id=run.run_id, content=message.content)],
    )
    try:
        await start_run(engine, thread, turn, user_text(message))
    except RunExists as error:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            RunConflict(message="The run already exists", run_id=run.run_id).model_dump(
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
    return turn


@router.post("/{agent}", response_class=EventSourceResponse)
async def run_agent(
    turn: Annotated[RunAgentInput, Depends(started_run)],
    served: Annotated[ServedAgent, Depends(served_agent)],
    engine: Annotated[AsyncEngine, Depends(database)],
) -> AsyncIterator[ServerSentEvent]:
    """Runs the recorded turn on the agent, streaming the run's AG-UI events"""
    async with aclosing(run_events(engine, served, turn)) as events:
        async for event in events:
            yield event


@router.get("/{agent}/runs/{run_id}")
async def run_status(
    run_id: str,
    claims: Annotated[Claims, Depends(verified_claims)],
    served: Annotated[ServedAgent, Depends(served_agent)],
    engine: Annotated[AsyncEngine, Depends(database)],
) -> RunView:
    """Where one of the caller's runs with the agent is; 404 when it has none by
    that id"""
    found = await find_run(
        engine, served.name, claims.client_id, claims.user_id, run_id
    )
    if found is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"No run {run_id}")
    return found


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
