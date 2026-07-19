"""baseline (empty)

Revision ID: 0001_baseline
Revises:
Create Date: 2026-07-19
"""

from __future__ import annotations

from collections.abc import Sequence

revision: str = "0001_baseline"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Intentionally empty. Establishes the migration chain's root so that
    later milestones (node/job/lease tables) have a real baseline to build
    on, instead of an implicit/unversioned schema."""
    pass


def downgrade() -> None:
    pass
