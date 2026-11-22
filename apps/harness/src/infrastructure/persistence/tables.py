from enum import StrEnum

from sqlalchemy import (
    BigInteger,
    Column,
    DateTime,
    ForeignKey,
    Identity,
    Index,
    Integer,
    MetaData,
    Table,
    Text,
    UniqueConstraint,
    Uuid,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB


class RunStatus(StrEnum):
    running = "running"
    """The run is streaming its events"""

    finished = "finished"
    """The run ended with RUN_FINISHED"""

    cancelled = "cancelled"
    """The client left before the run sent RUN_FINISHED or RUN_ERROR"""

    error = "error"
    """The run ended with RUN_ERROR"""


class RunErrorCode(StrEnum):
    recursion_limit = "recursion_limit"
    """The run took more graph steps than its recursion limit"""

    model_error = "model_error"
    """The model, or the graph around it, failed"""

    tool_error = "tool_error"
    """A tool raised, or opening the graph and what the run holds open failed"""

    timeout = "timeout"
    """A call the run made timed out"""

    storage_error = "storage_error"
    """Recording one of the run's events in Postgres failed"""


class MessageRole(StrEnum):
    user = "user"
    """The user's text"""

    assistant = "assistant"
    """The agent's final answer"""


metadata = MetaData()
"""The harness's own tables, which its Alembic migrations create"""

threads = Table(
    "threads",
    metadata,
    Column("id", Uuid, primary_key=True, server_default=func.gen_random_uuid()),
    Column("agent", Text, nullable=False),
    Column("client_id", Text, nullable=False),
    Column("user_id", Uuid, nullable=False),
    Column(
        "created_at", DateTime(timezone=True), nullable=False, server_default=func.now()
    ),
    UniqueConstraint("agent", "client_id", "user_id"),
)
"""One lifelong conversation of a user with an agent, through one client"""

runs = Table(
    "runs",
    metadata,
    Column("id", Text, primary_key=True),
    Column("thread_id", Uuid, ForeignKey("threads.id"), nullable=False),
    Column("status", Text, nullable=False),
    Column("error_code", Text),
    Column("error", Text),
    Column("input", JSONB, nullable=False),
    Column(
        "created_at", DateTime(timezone=True), nullable=False, server_default=func.now()
    ),
    Column("first_token_at", DateTime(timezone=True)),
    Column("finished_at", DateTime(timezone=True)),
    Index(
        "ix_runs_running_thread_id",
        "thread_id",
        unique=True,
        postgresql_where=text("status = 'running'"),
    ),
)
"""Each run of an agent on a thread, with the turn it ran"""

run_events = Table(
    "run_events",
    metadata,
    Column("id", BigInteger, Identity(), primary_key=True),
    Column("run_id", Text, ForeignKey("runs.id", ondelete="CASCADE"), nullable=False),
    Column("agent", Text, nullable=False),
    Column("type", Text, nullable=False),
    Column("event", JSONB, nullable=False),
    Column(
        "created_at", DateTime(timezone=True), nullable=False, server_default=func.now()
    ),
    Index("ix_run_events_run_id_id", "run_id", "id"),
)
"""Every AG-UI event a run sent, in order"""

messages = Table(
    "messages",
    metadata,
    Column("id", Uuid, primary_key=True),
    Column("thread_id", Uuid, ForeignKey("threads.id"), nullable=False),
    Column("run_id", Text, ForeignKey("runs.id", ondelete="CASCADE"), nullable=False),
    Column("role", Text, nullable=False),
    Column("content", Text, nullable=False),
    Column(
        "created_at", DateTime(timezone=True), nullable=False, server_default=func.now()
    ),
    Index("ix_messages_thread_id_created_at", "thread_id", "created_at"),
)
"""The transcript: each run's user text and the agent's final answer"""

run_model_usage = Table(
    "run_model_usage",
    metadata,
    Column("id", BigInteger, Identity(), primary_key=True),
    Column("run_id", Text, ForeignKey("runs.id", ondelete="CASCADE"), nullable=False),
    Column("agent", Text, nullable=False),
    Column("provider", Text, nullable=False),
    Column("model", Text),
    Column("input_tokens", Integer, nullable=False),
    Column("output_tokens", Integer, nullable=False),
    Column("reasoning_tokens", Integer, nullable=False),
    Column("cached_input_tokens", Integer, nullable=False),
    Column("cache_write_input_tokens", Integer, nullable=False),
    UniqueConstraint(
        "run_id", "agent", "provider", "model", postgresql_nulls_not_distinct=True
    ),
)
"""Tokens each agent of a run spent on each model"""

run_tool_usage = Table(
    "run_tool_usage",
    metadata,
    Column("id", BigInteger, Identity(), primary_key=True),
    Column("run_id", Text, ForeignKey("runs.id", ondelete="CASCADE"), nullable=False),
    Column("agent", Text, nullable=False),
    Column("tool", Text, nullable=False),
    Column("provider", Text),
    Column("calls", Integer, nullable=False),
    Column("failures", Integer, nullable=False),
    Column("duration_ms", Integer, nullable=False),
    UniqueConstraint(
        "run_id", "agent", "tool", "provider", postgresql_nulls_not_distinct=True
    ),
)
"""How often each agent of a run called each tool, how often it failed and how
long it took"""
