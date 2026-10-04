"""Dataset names are unique per uploader, not across everyone

Uploading was admin-only, so a global unique name was harmless. Any user may
now upload (services/ownership.py), and a global rule would let one user learn
what another had named their data from a "name taken" error -- and would make
two people who both call their upload "cats" collide for no reason.

The plain index on ``name`` is kept for lookups; uniqueness moves to
``(created_by, name)``.

Revision ID: 0015_dataset_names_per_owner
Revises: 0014_node_decommission
Create Date: 2026-10-04
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0015_dataset_names_per_owner"
down_revision: str | None = "0014_node_decommission"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.drop_index("ix_datasets_name", table_name="datasets")
    op.create_index("ix_datasets_name", "datasets", ["name"], unique=False)
    op.create_index(
        "ix_datasets_owner_name", "datasets", ["created_by", "name"], unique=True
    )


def downgrade() -> None:
    op.drop_index("ix_datasets_owner_name", table_name="datasets")
    op.drop_index("ix_datasets_name", table_name="datasets")
    op.create_index("ix_datasets_name", "datasets", ["name"], unique=True)
