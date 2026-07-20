"""M2 job model, lease protocol, scheduler state

Creates the job_state and lease_state enums, the jobs / job_events / leases
tables, the partial unique index enforcing one ACTIVE lease per job (ADR-003
defense in depth), and adds nodes.last_assigned_at for RoundRobin rotation.
Builds on the M1 identity/node schema (0002). Upgrades and downgrades cleanly.

Revision ID: 0003_m2_jobs_leases
Revises: 0002_m1_identity_nodes
Create Date: 2026-07-20
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0003_m2_jobs_leases"
down_revision: str | None = "0002_m1_identity_nodes"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Native PG enums; created/dropped explicitly so downgrade is clean.
_job_state = postgresql.ENUM(
    "QUEUED",
    "SCHEDULED",
    "LEASED",
    "RUNNING",
    "COMPLETED",
    "FAILED",
    "REASSIGNED",
    "CANCELLED",
    name="job_state",
    create_type=False,
)
_lease_state = postgresql.ENUM(
    "PENDING",
    "ACTIVE",
    "EXPIRED",
    "RELEASED",
    "COMPLETED",
    "FAILED",
    name="lease_state",
    create_type=False,
)


def upgrade() -> None:
    bind = op.get_bind()
    _job_state.create(bind, checkfirst=True)
    _lease_state.create(bind, checkfirst=True)

    # RoundRobin rotation state on the existing nodes table.
    op.add_column(
        "nodes",
        sa.Column("last_assigned_at", sa.DateTime(timezone=True), nullable=True),
    )

    op.create_table(
        "jobs",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("spec", postgresql.JSONB(), nullable=False),
        sa.Column("scheduler_name", sa.String(length=32), nullable=False),
        sa.Column("state", _job_state, nullable=False),
        sa.Column(
            "current_lease_epoch", sa.Integer(), server_default="0", nullable=False
        ),
        sa.Column(
            "scheduled_node_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("nodes.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("submitted_by", sa.String(length=255), nullable=False),
        sa.Column(
            "submitted_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("failure_reason", sa.String(length=1024), nullable=True),
    )
    op.create_index("ix_jobs_scheduled_node_id", "jobs", ["scheduled_node_id"])
    op.create_index("ix_jobs_submitted_at", "jobs", ["submitted_at"])
    # Supports the scheduler pass (jobs awaiting placement) and the claim query
    # (jobs scheduled to a given node).
    op.create_index("ix_jobs_state", "jobs", ["state"])

    op.create_table(
        "job_events",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column(
            "job_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("jobs.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "ts",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("from_state", _job_state, nullable=True),
        sa.Column("to_state", _job_state, nullable=False),
        sa.Column("detail", postgresql.JSONB(), nullable=False),
    )
    op.create_index("ix_job_events_job_id", "job_events", ["job_id"])
    op.create_index("ix_job_events_ts", "job_events", ["ts"])

    op.create_table(
        "leases",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "job_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("jobs.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "node_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("nodes.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("lease_epoch", sa.Integer(), nullable=False),
        sa.Column("state", _lease_state, nullable=False),
        sa.Column(
            "granted_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("renewed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("released_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_leases_job_id", "leases", ["job_id"])
    op.create_index("ix_leases_node_id", "leases", ["node_id"])
    # Sweep query: ACTIVE leases past their TTL.
    op.create_index("ix_leases_state_expires_at", "leases", ["state", "expires_at"])
    # ADR-003 defense in depth: at most one ACTIVE lease per job, enforced by
    # the database itself.
    op.create_index(
        "uq_one_active_lease_per_job",
        "leases",
        ["job_id"],
        unique=True,
        postgresql_where=sa.text("state = 'ACTIVE'"),
    )


def downgrade() -> None:
    op.drop_index("uq_one_active_lease_per_job", table_name="leases")
    op.drop_index("ix_leases_state_expires_at", table_name="leases")
    op.drop_index("ix_leases_node_id", table_name="leases")
    op.drop_index("ix_leases_job_id", table_name="leases")
    op.drop_table("leases")

    op.drop_index("ix_job_events_ts", table_name="job_events")
    op.drop_index("ix_job_events_job_id", table_name="job_events")
    op.drop_table("job_events")

    op.drop_index("ix_jobs_state", table_name="jobs")
    op.drop_index("ix_jobs_submitted_at", table_name="jobs")
    op.drop_index("ix_jobs_scheduled_node_id", table_name="jobs")
    op.drop_table("jobs")

    op.drop_column("nodes", "last_assigned_at")

    _lease_state.drop(op.get_bind(), checkfirst=True)
    _job_state.drop(op.get_bind(), checkfirst=True)
