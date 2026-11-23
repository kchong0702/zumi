import asyncio
import math
import socket
from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
from contextlib import asynccontextmanager, suppress
from datetime import UTC, datetime
from uuid import UUID, uuid4

import structlog
from ag_ui.core import RunAgentInput
from fastapi.sse import ServerSentEvent
from sqlalchemy.ext.asyncio import AsyncEngine

from src.app.agui.events import end_run, record_run, run_started, stale_end
from src.app.agui.served_agent import ServedAgent
from src.core.config import DatabaseSettings, RunSettings
from src.infrastructure.persistence import (
    RunChannel,
    StaleRun,
    claim_stale,
    events_after,
    heartbeat,
    release,
    run_notifications,
    start_run,
)


async def _every(seconds: float, step: Callable[[], Awaitable[None]]) -> None:
    """Runs step every seconds, logging any step that fails and carrying on"""
    while True:
        await asyncio.sleep(seconds)
        try:
            await step()
        except Exception:
            structlog.stdlib.get_logger().exception("run upkeep failed")


class RunnerFull(Exception):
    def __init__(self, retry_after_s: int) -> None:
        """Says how long to wait before starting a run on this process again"""
        super().__init__("this process works on as many runs as it may")
        self.retry_after_s = retry_after_s


class Runner:
    def __init__(
        self,
        engine: AsyncEngine,
        upkeep: AsyncEngine,
        database: DatabaseSettings,
        settings: RunSettings,
        agents: Sequence[ServedAgent],
    ) -> None:
        """Works on agents' runs in this process, apart from the requests that start
        or read them, and takes over the runs of processes that stopped"""
        self.owner = f"{socket.gethostname()}:{uuid4().hex[:8]}"
        """This process's name in runs.owner: its host and a suffix new each start"""

        self._engine = engine
        """The engine on the harness's tables that requests and workers use"""

        self._upkeep = upkeep
        """The engine that marks runs alive, sweeps and hands runs over"""

        self._database = database
        """Where to listen for run notifications"""

        self._settings = settings
        """How often to mark runs alive and sweep, and when to give up on a run"""

        self._agents = {served.name: served for served in agents}
        """The agents this process serves, by name"""

        self._workers: dict[str, asyncio.Task[None]] = {}
        """The task recording each run this process works on, until the run ends"""

        self._tasks: set[asyncio.Task[None]] = set()
        """Every worker task until it stops, closing what its run held included"""

        self._cancels: set[str] = set()
        """The runs whose workers this process cancelled because someone asked"""

        self._starting = 0
        """How many runs this process is recording before it works on them"""

        self._readers: dict[str, set[asyncio.Event]] = {}
        """What wakes each reader of a run when the run records an event"""

    def _forget(self, run_id: str, task: asyncio.Task[None]) -> None:
        """Drops the run's worker once it stopped, logging what it failed with"""
        if self._workers.get(run_id) is task:
            del self._workers[run_id]
        self._tasks.discard(task)
        self._cancels.discard(run_id)
        if not task.cancelled() and (error := task.exception()):
            structlog.stdlib.get_logger().error(
                "run worker failed", run_id=run_id, exc_info=error
            )

    def _work(
        self,
        served: ServedAgent,
        turn: RunAgentInput,
        started_at: datetime,
        resumed: bool,
    ) -> None:
        """Starts the task that records the turn's run on the agent"""
        task = asyncio.create_task(
            record_run(
                self._engine,
                self.owner,
                served,
                turn,
                started_at,
                resumed,
                self._cancels,
                lambda: self._workers.pop(turn.run_id, None),
            )
        )
        self._workers[turn.run_id] = task
        self._tasks.add(task)
        task.add_done_callback(lambda done: self._forget(turn.run_id, done))

    async def start(
        self, served: ServedAgent, thread: UUID, turn: RunAgentInput, user_text: str
    ) -> None:
        """Records the turn's new run on the agent with its RUN_STARTED and works on it;
        raises RunnerFull while this process works on max_workers runs"""
        if len(self._workers) + self._starting >= self._settings.max_workers:
            raise RunnerFull(self._settings.retry_after_s)
        self._starting += 1
        try:
            await start_run(
                self._engine,
                thread,
                turn,
                user_text,
                self.owner,
                served.name,
                run_started(served.name, turn),
            )
        finally:
            self._starting -= 1
        self._work(served, turn, datetime.now(UTC), resumed=False)

    def _cancel(self, run_id: str) -> None:
        """Cancels this process's worker on the run, which ends it as cancelled"""
        if run_id in self._workers and run_id not in self._cancels:
            self._cancels.add(run_id)
            self._workers[run_id].cancel()

    async def _beat(self) -> None:
        """Marks this process's runs alive, stops the workers of the runs another
        process took and cancels the runs someone asked to cancel"""
        if not self._workers:
            return
        beat = await heartbeat(self._upkeep, self.owner, list(self._workers))
        for run_id in beat.lost & self._workers.keys():
            structlog.stdlib.get_logger().warning(
                "run lost to another process", run_id=run_id
            )
            self._workers[run_id].cancel()
        for run_id in beat.cancel_requested:
            self._cancel(run_id)

    async def sweep(self) -> None:
        """Takes over the runs no process marked alive lately, as many as this process
        has free workers for: resumes them, or ends those that can't resume"""
        free = self._settings.max_workers - len(self._workers) - self._starting
        if free <= 0:
            return
        for run in await claim_stale(
            self._upkeep,
            self.owner,
            list(self._agents),
            self._settings.dead_after_s,
            self._settings.resume_within_s,
            free,
        ):
            if run.id in self._workers:
                continue
            structlog.stdlib.get_logger().warning(
                "run taken over", agent=run.agent, run_id=run.id
            )
            served = self._agents[run.agent]
            ending = stale_end(run, self._settings.max_attempts)
            if ending is not None:
                await end_run(self._upkeep, self.owner, served, run, ending)
            elif isinstance(run, StaleRun):
                self._work(served, run.input, run.created_at, resumed=True)

    async def _listen(self) -> None:
        """Wakes the readers of runs that record events and cancels the runs someone
        asked to cancel, listening again with backoff after the connection fails"""
        backoff_s = 1.0
        while True:
            try:
                async for channel, run_id in run_notifications(
                    self._database, math.ceil(self._settings.heartbeat_s)
                ):
                    backoff_s = 1.0
                    if channel == RunChannel.cancels:
                        self._cancel(run_id)
                    for woken in self._readers.get(run_id, ()):
                        woken.set()
            except Exception:
                structlog.stdlib.get_logger().exception(
                    "listening for run notifications failed"
                )
            await asyncio.sleep(backoff_s)
            backoff_s = min(backoff_s * 2, self._settings.poll_s)

    async def stop(self) -> None:
        """Stops this process's workers, leaving their runs running for another process
        to take over, and waits for ended runs to close what they held"""
        for task in self._workers.values():
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)

    @asynccontextmanager
    async def kept(self) -> AsyncIterator[None]:
        """Takes over stale runs, then listens, marks this process's runs alive and
        sweeps until closed; then stops the workers and hands their runs over"""
        try:
            await self.sweep()
        except Exception:
            structlog.stdlib.get_logger().exception("run upkeep failed")
        upkeep = [
            asyncio.create_task(self._listen()),
            asyncio.create_task(_every(self._settings.heartbeat_s, self._beat)),
            asyncio.create_task(_every(self._settings.sweep_s, self.sweep)),
        ]
        try:
            yield
        finally:
            for task in upkeep:
                task.cancel()
            await asyncio.gather(*upkeep, return_exceptions=True)
            await self.stop()
            await release(self._upkeep, self.owner)

    async def events(self, run_id: str, after: int) -> AsyncIterator[ServerSentEvent]:
        """The run's events recorded after the event id after, then each new one as it
        is recorded until the run ends, as SSE whose ids are the events' ids"""
        woken = asyncio.Event()
        self._readers.setdefault(run_id, set()).add(woken)
        try:
            while True:
                woken.clear()
                running, events = await events_after(self._engine, run_id, after)
                for event in events:
                    yield ServerSentEvent(raw_data=event.data, id=str(event.id))
                    after = event.id
                if not running:
                    return
                with suppress(TimeoutError):
                    async with asyncio.timeout(self._settings.poll_s):
                        await woken.wait()
        finally:
            self._readers[run_id].discard(woken)
            if not self._readers[run_id]:
                del self._readers[run_id]
