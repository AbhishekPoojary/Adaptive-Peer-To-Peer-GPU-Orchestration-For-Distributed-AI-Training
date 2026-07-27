"""M8 security hardening: human operator accounts and enrollment-token revocation

Adds the ``users`` table backing ADR-012's human identity layer (password +
``aud="user"`` JWT), which is distinct from ADR-008's machine identity for
nodes, and gives ``enrollment_tokens`` an explicit ``revoked_at`` so a minted
token can be withdrawn before it is used or expires.

No user rows are created here. A migration that seeded an account would be
baking a credential into the schema; bootstrap is an explicit, auditable
operator action (``scripts/create_user.py``) that reads the password from a
prompt or the environment and never from argv.

Revision ID: 0008_m8_users
Revises: 0007_unclaimed_lease_state
Create Date: 2026-07-27
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0008_m8_users"
down_revision: str | None = "0007_unclaimed_lease_state"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_ROLE_LABELS = ("ADMIN", "OPERATOR")


def upgrade() -> None:
    user_role = postgresql.ENUM(*_ROLE_LABELS, name="user_role", create_type=False)
    user_role.create(op.get_bind(), checkfirst=True)

    op.create_table(
        "users",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("username", sa.String(64), nullable=False),
        # Wide enough for the scrypt format string with room for future cost
        # parameters; the plaintext password is never stored.
        sa.Column("password_hash", sa.String(256), nullable=False),
        sa.Column("role", user_role, nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("disabled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_login_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_users_username", "users", ["username"], unique=True)

    # Explicit revocation for a token that was minted but should no longer
    # work. Distinct from used_at (consumed) and expires_at (aged out) so the
    # audit trail says which of the three actually happened.
    op.add_column(
        "enrollment_tokens",
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("enrollment_tokens", "revoked_at")
    op.drop_index("ix_users_username", table_name="users")
    op.drop_table("users")
    postgresql.ENUM(name="user_role").drop(op.get_bind(), checkfirst=True)
