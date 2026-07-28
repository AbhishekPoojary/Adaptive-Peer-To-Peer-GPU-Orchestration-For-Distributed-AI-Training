"""Back off from a node that lets an offer lapse unclaimed

Adds ``nodes.claim_backoff_until``. When a PENDING lease expires without ever
being claimed, the scheduler stops offering that node work until this timestamp
passes.

Deliberately *not* a reliability signal. An unclaimed offer is blameless
(revision 0007, ADR-003 addendum) — reliability counters stay untouched and
``UNCLAIMED`` remains excluded from the adaptive scheduler's history inputs.
This column expresses something narrower and shorter-lived: "this node did not
pick up the last thing it was handed, so do not hand it another one for a
moment." It is a scheduling hint that ages out on its own, not a mark on the
node's record.

Nullable with no default, because "has never lapsed an offer" is a real state
distinct from "backed off until some time in the past", and every existing row
genuinely has no backoff.

Revision ID: 0009_unclaimed_backoff
Revises: 0008_m8_users
Create Date: 2026-07-28
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0009_unclaimed_backoff"
down_revision: str | None = "0008_m8_users"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "nodes",
        sa.Column("claim_backoff_until", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("nodes", "claim_backoff_until")
