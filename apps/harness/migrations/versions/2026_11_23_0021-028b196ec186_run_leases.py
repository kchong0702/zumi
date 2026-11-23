"""run leases

Revision ID: 028b196ec186
Revises: 0c287aa5ca8d
Create Date: 2026-11-23 00:21:25.577414

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "028b196ec186"
down_revision: str | Sequence[str] | None = "0c287aa5ca8d"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column("runs", sa.Column("owner", sa.Text(), nullable=True))
    op.add_column(
        "runs", sa.Column("heartbeat_at", sa.DateTime(timezone=True), nullable=True)
    )
    op.add_column(
        "runs",
        sa.Column("cancel_requested_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "runs",
        sa.Column(
            "attempts", sa.Integer(), server_default=sa.text("0"), nullable=False
        ),
    )
    op.create_index(
        "ix_runs_running_heartbeat_at",
        "runs",
        ["heartbeat_at"],
        unique=False,
        postgresql_where=sa.text("status = 'running'"),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index(
        "ix_runs_running_heartbeat_at",
        table_name="runs",
        postgresql_where=sa.text("status = 'running'"),
    )
    op.drop_column("runs", "attempts")
    op.drop_column("runs", "cancel_requested_at")
    op.drop_column("runs", "heartbeat_at")
    op.drop_column("runs", "owner")
