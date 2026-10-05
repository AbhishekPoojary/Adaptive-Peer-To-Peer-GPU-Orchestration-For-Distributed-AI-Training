"""Who added each machine

Adds ``nodes.enrolled_by``: the username on the enrollment token a node used.
Any signed-in user may now lend their own computer ("Lend my computer"), and
this is what lets them see it in their list and remove it again without an
admin. Existing nodes get NULL -- who enrolled them was never recorded, and
guessing would hand someone a machine that is not theirs.

Revision ID: 0016_node_enrolled_by
Revises: 0015_dataset_names_per_owner
Create Date: 2026-10-05
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0016_node_enrolled_by"
down_revision: str | None = "0015_dataset_names_per_owner"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("nodes", sa.Column("enrolled_by", sa.String(255), nullable=True))


def downgrade() -> None:
    op.drop_column("nodes", "enrolled_by")
