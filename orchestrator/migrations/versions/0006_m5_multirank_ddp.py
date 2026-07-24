"""M5 multi-rank DDP: lease rank + cohort fencing, rendezvous host

Generalises the M2 single-lease-per-job model to N-rank training cohorts
(ADR-005 distributed training, ADR-003 epoch fencing extended to a whole
cohort):

* ``leases.rank`` — the 0..N-1 slot a lease fills within its job's attempt.
  Single-rank (world_size=1) jobs are simply rank 0, so nothing about the M2
  path changes semantically.
* The partial unique index that enforced "one ACTIVE lease per job" is
  generalised to "one non-terminal (PENDING or ACTIVE) lease per (job, rank)":
  a cohort now legitimately holds N concurrent ACTIVE leases (one per rank),
  but never two for the same rank, and never a duplicate PENDING slot. Cohort
  slots are created PENDING at schedule time and flip to ACTIVE on claim
  (reusing the reserved ``LeaseState.PENDING``).
* ``jobs.rendezvous_node_id`` — which cohort member is the c10d rendezvous host
  for the current attempt (ADR-005: the highest-reliability member). NULL until
  a multi-node attempt is scheduled; SET NULL on node delete so history survives.

Builds on the M4 schema (0005). Upgrades and downgrades cleanly and can be
re-applied (no leftover index blocks recreation).

Revision ID: 0006_m5_multirank_ddp
Revises: 0005_m4_training_execution
Create Date: 2026-07-24
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0006_m5_multirank_ddp"
down_revision: str | None = "0005_m4_training_execution"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_OLD_INDEX = "uq_one_active_lease_per_job"
_NEW_INDEX = "uq_active_lease_per_job_rank"


def upgrade() -> None:
    # Rank slot within the job's attempt. server_default 0 backfills every
    # existing single-rank lease to rank 0 with no data migration.
    op.add_column(
        "leases",
        sa.Column("rank", sa.Integer(), server_default="0", nullable=False),
    )

    # Which cohort member hosts c10d rendezvous this attempt (ADR-005).
    op.add_column(
        "jobs",
        sa.Column(
            "rendezvous_node_id",
            sa.dialects.postgresql.UUID(as_uuid=True),
            sa.ForeignKey("nodes.id", ondelete="SET NULL"),
            nullable=True,
        ),
    )

    # Generalise the fencing index: at most one non-terminal lease per
    # (job, rank). A cohort holds N ACTIVE leases (distinct ranks) legitimately;
    # a duplicate rank — or a duplicate PENDING slot — is physically impossible.
    op.drop_index(_OLD_INDEX, table_name="leases")
    op.create_index(
        _NEW_INDEX,
        "leases",
        ["job_id", "rank"],
        unique=True,
        postgresql_where=sa.text("state IN ('PENDING', 'ACTIVE')"),
    )


def downgrade() -> None:
    op.drop_index(_NEW_INDEX, table_name="leases")
    # Restore the M2 index verbatim so a downgrade lands exactly on the 0005
    # schema shape.
    op.create_index(
        _OLD_INDEX,
        "leases",
        ["job_id"],
        unique=True,
        postgresql_where=sa.text("state = 'ACTIVE'"),
    )

    op.drop_column("jobs", "rendezvous_node_id")
    op.drop_column("leases", "rank")
