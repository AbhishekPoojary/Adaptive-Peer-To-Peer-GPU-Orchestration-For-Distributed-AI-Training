"""M1 identity + node schema

Creates enrollment tokens, nodes, node telemetry samples, and auth challenges,
plus the node_status enum and the node-name sequence. Builds on the empty
baseline (0001). Upgrades and downgrades cleanly.

Revision ID: 0002_m1_identity_nodes
Revises: 0001_baseline
Create Date: 2026-07-19
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0002_m1_identity_nodes"
down_revision: str | None = "0001_baseline"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Native PG enum; created/dropped explicitly so downgrade is clean.
_node_status = postgresql.ENUM(
    "ONLINE", "OFFLINE", "DRAINING", name="node_status", create_type=False
)


def upgrade() -> None:
    bind = op.get_bind()
    _node_status.create(bind, checkfirst=True)

    # Server-assigned node names ("node-01", ...) draw from this sequence so
    # concurrent enrollments never collide on a name.
    op.execute(sa.text("CREATE SEQUENCE IF NOT EXISTS node_name_seq START WITH 1"))

    op.create_table(
        "enrollment_tokens",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("token_hash", sa.String(length=64), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_by", sa.String(length=255), nullable=False),
    )
    op.create_index(
        "ix_enrollment_tokens_token_hash",
        "enrollment_tokens",
        ["token_hash"],
        unique=True,
    )

    op.create_table(
        "nodes",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("name", sa.String(length=64), nullable=False),
        sa.Column("public_key", sa.String(), nullable=False),
        sa.Column("status", _node_status, nullable=False),
        sa.Column(
            "enrolled_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("last_heartbeat_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("hardware", postgresql.JSONB(), nullable=False),
        sa.Column("agent_version", sa.String(length=64), nullable=False),
        sa.Column(
            "lease_success_count", sa.BigInteger(), server_default="0", nullable=False
        ),
        sa.Column(
            "lease_failure_count", sa.BigInteger(), server_default="0", nullable=False
        ),
        sa.Column(
            "reliability_prior_alpha", sa.Float(), server_default="1.0", nullable=False
        ),
        sa.Column(
            "reliability_prior_beta", sa.Float(), server_default="1.0", nullable=False
        ),
    )
    op.create_index("ix_nodes_name", "nodes", ["name"], unique=True)

    op.create_table(
        "node_telemetry_samples",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column(
            "node_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("nodes.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "ts",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("cpu_percent", sa.Float(), nullable=False),
        sa.Column("ram_used_bytes", sa.BigInteger(), nullable=False),
        sa.Column("ram_total_bytes", sa.BigInteger(), nullable=False),
        sa.Column("gpu", postgresql.JSONB(), nullable=True),
        sa.Column("rtt_ms", sa.Float(), nullable=True),
        sa.Column("rtt_ewma_ms", sa.Float(), nullable=True),
    )
    op.create_index(
        "ix_node_telemetry_samples_node_id", "node_telemetry_samples", ["node_id"]
    )
    op.create_index("ix_node_telemetry_samples_ts", "node_telemetry_samples", ["ts"])

    op.create_table(
        "auth_challenges",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "node_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("nodes.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("nonce", sa.String(length=64), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("used_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index(
        "ix_auth_challenges_nonce", "auth_challenges", ["nonce"], unique=True
    )
    op.create_index("ix_auth_challenges_node_id", "auth_challenges", ["node_id"])


def downgrade() -> None:
    op.drop_index("ix_auth_challenges_node_id", table_name="auth_challenges")
    op.drop_index("ix_auth_challenges_nonce", table_name="auth_challenges")
    op.drop_table("auth_challenges")

    op.drop_index(
        "ix_node_telemetry_samples_ts", table_name="node_telemetry_samples"
    )
    op.drop_index(
        "ix_node_telemetry_samples_node_id", table_name="node_telemetry_samples"
    )
    op.drop_table("node_telemetry_samples")

    op.drop_index("ix_nodes_name", table_name="nodes")
    op.drop_table("nodes")

    op.drop_index(
        "ix_enrollment_tokens_token_hash", table_name="enrollment_tokens"
    )
    op.drop_table("enrollment_tokens")

    op.execute(sa.text("DROP SEQUENCE IF EXISTS node_name_seq"))
    _node_status.drop(op.get_bind(), checkfirst=True)
