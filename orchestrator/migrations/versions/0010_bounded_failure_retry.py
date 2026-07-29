"""Bounded retry on a reported trainer failure

Two changes, both for ADR-005 addendum 2:

* ``jobs.failed_attempt_count`` — how many attempts have ended in a reported
  trainer failure. Deliberately separate from ``current_lease_epoch``, which
  also advances for dropped-node reassignments; conflating them would let node
  churn silently exhaust a job's failure budget.

* ``nodes.claim_backoff_until`` is renamed to ``nodes.scheduling_backoff_until``.
  M7.1c introduced it for one case — a node that let an offer lapse unclaimed —
  and the retry path now uses the same mechanism to steer a retry away from the
  node whose trainer just died. The column's real meaning was always "the
  scheduler should skip this node briefly", and the narrower name would have
  misdescribed it for every future reader. A rename is cheap here: the values
  are seconds-lived scheduling hints, so nothing of consequence is carried.

Revision ID: 0010_bounded_failure_retry
Revises: 0009_unclaimed_backoff
Create Date: 2026-07-28
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0010_bounded_failure_retry"
down_revision: str | None = "0009_unclaimed_backoff"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.alter_column(
        "nodes", "claim_backoff_until", new_column_name="scheduling_backoff_until"
    )
    op.add_column(
        "jobs",
        sa.Column(
            "failed_attempt_count",
            sa.Integer(),
            nullable=False,
            server_default="0",
        ),
    )


def downgrade() -> None:
    op.drop_column("jobs", "failed_attempt_count")
    op.alter_column(
        "nodes", "scheduling_backoff_until", new_column_name="claim_backoff_until"
    )
