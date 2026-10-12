"""Measured training speed, for the adaptive scheduler's speed term

``leases.samples_per_second``: training images per second the leaseholder
measured on a completed attempt. The scheduler learns each machine's speed from
these (``services.scheduling._speed_inputs``). The decision audit gains the
fourth term: ``scheduling_decisions.delta`` and, per candidate, ``c_score`` and
the ``raw_samples_per_second`` behind it. All nullable: nothing before this was
measured, and nothing is backfilled.

Revision ID: 0018_node_speed
Revises: 0017_node_addresses
Create Date: 2026-10-12
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0018_node_speed"
down_revision: str | None = "0017_node_addresses"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("leases", sa.Column("samples_per_second", sa.Float, nullable=True))
    op.add_column("scheduling_decisions", sa.Column("delta", sa.Float, nullable=True))
    op.add_column(
        "scheduling_decision_candidates", sa.Column("c_score", sa.Float, nullable=True)
    )
    op.add_column(
        "scheduling_decision_candidates",
        sa.Column("raw_samples_per_second", sa.Float, nullable=True),
    )


def downgrade() -> None:
    op.drop_column("scheduling_decision_candidates", "raw_samples_per_second")
    op.drop_column("scheduling_decision_candidates", "c_score")
    op.drop_column("scheduling_decisions", "delta")
    op.drop_column("leases", "samples_per_second")
