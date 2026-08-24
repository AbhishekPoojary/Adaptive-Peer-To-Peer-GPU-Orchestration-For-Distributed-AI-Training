"""Custom image-classification datasets (ADR-014)

Adds the ``datasets`` table recording an uploaded, validated archive: where it
lives in object storage, the SHA-256 a peer re-checks before extracting it, and
the class/split counts a human needs to tell two uploads apart.

The images themselves are **not** stored here. They live in the datasets bucket
in MinIO, because a peer fetches them directly — streaming gigabytes through the
control plane would make the orchestrator a bottleneck for the one operation
guaranteed to be large.

``deleted_at`` is a soft delete rather than a real one. A finished job records
which dataset it trained on, and removing the row would leave that job pointing
at nothing, turning a real training record into an unanswerable question. The
stored object is deleted; the row survives so the audit trail still resolves.

Nothing here touches ``jobs``. A job's dataset reference lives inside its
existing JSONB ``spec`` column, so no schema change is needed on that side and
every historical job keeps validating exactly as before.

Revision ID: 0012_custom_datasets
Revises: 0011_google_sign_in
Create Date: 2026-08-24
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0012_custom_datasets"
down_revision: str | None = "0011_google_sign_in"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "datasets",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("name", sa.String(128), nullable=False),
        sa.Column("description", sa.String(1024), nullable=True),
        # Server-assigned object key. A caller never supplies this, so a request
        # cannot be steered at another object in the bucket.
        sa.Column("object_key", sa.String(512), nullable=False),
        # Hex SHA-256 of the exact stored bytes; the peer recomputes it before
        # extracting, which is what carries the upload-time validation across to
        # the machine that actually runs the data.
        sa.Column("sha256", sa.String(64), nullable=False),
        sa.Column("size_bytes", sa.BigInteger, nullable=False),
        # Sorted class names. The trainer sizes its output layer from the length
        # of this list, so it is load-bearing rather than descriptive.
        sa.Column("classes", postgresql.JSONB, nullable=False),
        sa.Column("train_images", sa.Integer, nullable=False),
        sa.Column("test_images", sa.Integer, nullable=False),
        sa.Column("per_class_counts", postgresql.JSONB, nullable=False),
        sa.Column("created_by", sa.String(255), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
    )
    # Unique on name so a job that says it trained on "flowers" is still
    # unambiguous a month later. Enforced in the database rather than by a
    # check-then-insert two concurrent uploads could both pass.
    op.create_index("ix_datasets_name", "datasets", ["name"], unique=True)


def downgrade() -> None:
    op.drop_index("ix_datasets_name", table_name="datasets")
    op.drop_table("datasets")
