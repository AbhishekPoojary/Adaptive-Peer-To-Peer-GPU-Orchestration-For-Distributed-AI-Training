"""Where each machine can be reached, for one job across several machines

Adds ``nodes.lan_address`` (the address the agent reports on its own local
network) and ``nodes.public_address`` (the address its heartbeats arrive
from). A multi-machine job is only placed on machines sharing a public
address -- behind the same router, so they can reach each other -- and rank 0's
LAN address is where the other ranks connect. Both NULL until a heartbeat
from an agent new enough to report them.

Revision ID: 0017_node_addresses
Revises: 0016_node_enrolled_by
Create Date: 2026-10-12
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0017_node_addresses"
down_revision: str | None = "0016_node_enrolled_by"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("nodes", sa.Column("lan_address", sa.String(64), nullable=True))
    op.add_column("nodes", sa.Column("public_address", sa.String(64), nullable=True))


def downgrade() -> None:
    op.drop_column("nodes", "public_address")
    op.drop_column("nodes", "lan_address")
