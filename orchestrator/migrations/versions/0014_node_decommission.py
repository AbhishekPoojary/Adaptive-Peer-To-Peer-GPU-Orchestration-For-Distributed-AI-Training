"""Removing a node from the fleet

Adds ``nodes.decommissioned_at``. A removed node is hidden from the fleet list
and can no longer authenticate, but its row stays: leases, scheduling audits
and jobs all name it, and a finished job's record should still say which
machine trained it. Every machine that ever enrolled used to stay in the list
forever, so a long-lived deployment's Overview read ``0 / N`` with a
denominator of dead laptops.

Revision ID: 0014_node_decommission
Revises: 0013_telemetry_node_ts_index
Create Date: 2026-10-03
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0014_node_decommission"
down_revision: str | None = "0013_telemetry_node_ts_index"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "nodes",
        sa.Column("decommissioned_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("nodes", "decommissioned_at")
