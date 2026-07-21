"""M3 adaptive scheduler audit trail

Creates the scheduling_decisions and scheduling_decision_candidates tables
(ADR-009): one decision row per adaptive placement plus one candidate row per
node considered, carrying the L/R/D/S scores and the raw inputs behind them.
Builds on the M2 job/lease schema (0003). Upgrades and downgrades cleanly and
can be re-applied. No new enums — the candidate flag is a plain boolean.

Revision ID: 0004_m3_scheduling_decisions
Revises: 0003_m2_jobs_leases
Create Date: 2026-07-22
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0004_m3_scheduling_decisions"
down_revision: str | None = "0003_m2_jobs_leases"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "scheduling_decisions",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
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
        sa.Column("scheduler_name", sa.String(length=32), nullable=False),
        sa.Column("alpha", sa.Float(), nullable=False),
        sa.Column("beta", sa.Float(), nullable=False),
        sa.Column("gamma", sa.Float(), nullable=False),
        sa.Column("reliability_halflife_seconds", sa.Float(), nullable=False),
        sa.Column("wilson_z", sa.Float(), nullable=False),
        sa.Column(
            "selected_node_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("nodes.id", ondelete="SET NULL"),
            nullable=True,
        ),
    )
    op.create_index(
        "ix_scheduling_decisions_job_id", "scheduling_decisions", ["job_id"]
    )
    op.create_index("ix_scheduling_decisions_ts", "scheduling_decisions", ["ts"])

    op.create_table(
        "scheduling_decision_candidates",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column(
            "decision_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("scheduling_decisions.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "node_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("nodes.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("node_name", sa.String(length=64), nullable=False),
        sa.Column("l_score", sa.Float(), nullable=False),
        sa.Column("r_score", sa.Float(), nullable=False),
        sa.Column("d_score", sa.Float(), nullable=False),
        sa.Column("s_score", sa.Float(), nullable=False),
        sa.Column("raw_util", sa.Float(), nullable=True),
        sa.Column("raw_rtt_ewma_ms", sa.Float(), nullable=True),
        sa.Column("weighted_success", sa.Float(), nullable=False),
        sa.Column("weighted_failure", sa.Float(), nullable=False),
        sa.Column("was_selected", sa.Boolean(), nullable=False),
    )
    op.create_index(
        "ix_scheduling_decision_candidates_decision_id",
        "scheduling_decision_candidates",
        ["decision_id"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_scheduling_decision_candidates_decision_id",
        table_name="scheduling_decision_candidates",
    )
    op.drop_table("scheduling_decision_candidates")

    op.drop_index("ix_scheduling_decisions_ts", table_name="scheduling_decisions")
    op.drop_index("ix_scheduling_decisions_job_id", table_name="scheduling_decisions")
    op.drop_table("scheduling_decisions")
