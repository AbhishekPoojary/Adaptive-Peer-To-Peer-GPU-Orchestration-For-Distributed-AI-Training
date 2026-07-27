"""Add the UNCLAIMED terminal lease state

An offer that was never picked up is not a node failure. Before this revision
the TTL sweep collapsed two different events into ``EXPIRED``:

* an ACTIVE lease that passed its TTL — the node took work on and stopped
  making progress (a real reliability signal, ADR-009), and
* a PENDING cohort slot that was never claimed — the *scheduler* offered work
  nobody picked up.

``UNCLAIMED`` gives the second event its own terminal state, so reliability
(which is computed straight from ``leases`` rows in
``services.scheduling._reliability_inputs``) can exclude it without inventing a
heuristic. See ``docs/adr/ADR-003-addendum.md``.

Schema-only: existing rows are untouched here. The one-off reclassification of
already-recorded rows is a separate, auditable maintenance command
(``scripts/repair_reliability_counts.py``), not a silent data rewrite hidden in
a migration.

Downgrade folds any UNCLAIMED row back into EXPIRED (the pre-0007
representation) and rebuilds the enum without the label.

Revision ID: 0007_unclaimed_lease_state
Revises: 0006_m5_multirank_ddp
Create Date: 2026-07-27
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0007_unclaimed_lease_state"
down_revision: str | None = "0006_m5_multirank_ddp"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_INDEX = "uq_active_lease_per_job_rank"
_PRE_0007_LABELS = ("PENDING", "ACTIVE", "EXPIRED", "RELEASED", "COMPLETED", "FAILED")


def upgrade() -> None:
    # IF NOT EXISTS keeps the revision re-appliable. Postgres 12+ permits
    # ALTER TYPE ... ADD VALUE inside a transaction as long as the new label is
    # not *used* in the same transaction; nothing here writes it.
    op.execute("ALTER TYPE lease_state ADD VALUE IF NOT EXISTS 'UNCLAIMED'")


def downgrade() -> None:
    # Postgres cannot drop a single enum label, so the type is rebuilt. The
    # partial unique index is dropped and recreated around the column type swap
    # because its predicate is stored against the column's type.
    labels = ", ".join(f"'{label}'" for label in _PRE_0007_LABELS)
    op.drop_index(_INDEX, table_name="leases")
    op.execute("ALTER TYPE lease_state RENAME TO lease_state_old")
    op.execute(f"CREATE TYPE lease_state AS ENUM ({labels})")
    op.execute(
        "ALTER TABLE leases ALTER COLUMN state TYPE lease_state USING "
        "(CASE WHEN state::text = 'UNCLAIMED' THEN 'EXPIRED' ELSE state::text END)"
        "::lease_state"
    )
    op.execute("DROP TYPE lease_state_old")
    op.create_index(
        _INDEX,
        "leases",
        ["job_id", "rank"],
        unique=True,
        postgresql_where=sa.text("state IN ('PENDING', 'ACTIVE')"),
    )
