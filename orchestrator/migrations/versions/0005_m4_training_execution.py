"""M4 training execution: job.result, training_metrics, training_log_lines

Adds ``jobs.result`` (JSONB, nullable — populated only when a real lease
outcome carries a training result summary) and two new tables fed by the
WebSocket log/metric stream (ADR-002): ``training_metrics`` (one row per real
parsed per-epoch metric line) and ``training_log_lines`` (the raw stdout/
stderr transcript, capped per job by the service layer). Builds on the M3
scheduling schema (0004). Upgrades and downgrades cleanly.

Revision ID: 0005_m4_training_execution
Revises: 0004_m3_scheduling_decisions
Create Date: 2026-07-23
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0005_m4_training_execution"
down_revision: str | None = "0004_m3_scheduling_decisions"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("jobs", sa.Column("result", postgresql.JSONB(), nullable=True))

    op.create_table(
        "training_metrics",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column(
            "job_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("jobs.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "lease_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("leases.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("epoch", sa.Integer(), nullable=False),
        sa.Column("step", sa.Integer(), nullable=True),
        sa.Column("loss", sa.Float(), nullable=False),
        sa.Column("test_accuracy", sa.Float(), nullable=True),
        sa.Column(
            "ts",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
    )
    op.create_index("ix_training_metrics_job_id", "training_metrics", ["job_id"])
    op.create_index("ix_training_metrics_lease_id", "training_metrics", ["lease_id"])
    op.create_index("ix_training_metrics_ts", "training_metrics", ["ts"])

    op.create_table(
        "training_log_lines",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column(
            "job_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("jobs.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "lease_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("leases.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "ts",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("stream", sa.String(length=16), nullable=False),
        sa.Column("line", sa.String(), nullable=False),
    )
    op.create_index("ix_training_log_lines_job_id", "training_log_lines", ["job_id"])
    op.create_index("ix_training_log_lines_lease_id", "training_log_lines", ["lease_id"])
    op.create_index("ix_training_log_lines_ts", "training_log_lines", ["ts"])
    # Supports the retention trim's ORDER BY id DESC LIMIT per job.
    op.create_index(
        "ix_training_log_lines_job_id_id", "training_log_lines", ["job_id", "id"]
    )


def downgrade() -> None:
    op.drop_index("ix_training_log_lines_job_id_id", table_name="training_log_lines")
    op.drop_index("ix_training_log_lines_ts", table_name="training_log_lines")
    op.drop_index("ix_training_log_lines_lease_id", table_name="training_log_lines")
    op.drop_index("ix_training_log_lines_job_id", table_name="training_log_lines")
    op.drop_table("training_log_lines")

    op.drop_index("ix_training_metrics_ts", table_name="training_metrics")
    op.drop_index("ix_training_metrics_lease_id", table_name="training_metrics")
    op.drop_index("ix_training_metrics_job_id", table_name="training_metrics")
    op.drop_table("training_metrics")

    op.drop_column("jobs", "result")
