"""A per-node, time-ordered index on telemetry samples

Two hot queries ask for one node's most recent samples: every heartbeat reads
the previous RTT EWMA, and every failure-detector pass (twice a second) reads
each ONLINE node's last 21 arrival times. With only single-column indexes on
``node_id`` and ``ts``, Postgres answered both by walking the ``ts`` index
backwards across *every* node's samples and filtering for this one:

    Index Scan Backward using ix_node_telemetry_samples_ts
      Filter: (node_id = ...)   Rows Removed by Filter: 4017

So each heartbeat's cost grew with the whole fleet's history, and one row is
added per heartbeat, forever. ``(node_id, ts, id)`` lets both queries jump
straight to the node's newest rows. ``id`` is included because both queries
break ties on it.

Revision ID: 0013_telemetry_node_ts_index
Revises: 0012_custom_datasets
Create Date: 2026-10-03
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0013_telemetry_node_ts_index"
down_revision: str | None = "0012_custom_datasets"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_index(
        "ix_node_telemetry_samples_node_id_ts",
        "node_telemetry_samples",
        ["node_id", "ts", "id"],
    )


def downgrade() -> None:
    op.drop_index("ix_node_telemetry_samples_node_id_ts", table_name="node_telemetry_samples")
