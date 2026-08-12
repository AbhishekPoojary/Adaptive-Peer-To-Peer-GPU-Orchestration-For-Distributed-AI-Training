"""Google sign-in: verified email + Google subject on users (ADR-012 addendum)

Adds the two columns a Google identity is matched against, and relaxes
``password_hash`` to nullable so an account can exist that signs in only with
Google and therefore has no password.

Nullable ``password_hash`` is the one change here worth pausing on. It does
**not** create accounts that authenticate with an empty password: NULL is read
by ``services.users.authenticate_user`` as "this account has no password", which
refuses the password path outright while still spending the KDF so the timing
does not reveal which kind of account it is. Making the column nullable is what
lets that refusal be represented at all; the alternative — storing a hash of an
unguessable random string — would leave a credential in the row that nobody can
use and nobody can audit.

No row is backfilled. Every existing account keeps its password and gets NULL
for ``email`` and ``google_sub``, so nothing can sign in with Google until an
admin sets an email on purpose. Guessing which Google address belongs to an
existing username would be fabricating an identity binding, and this system
treats an account as permission to run containers on other people's machines.

Both new columns are uniquely indexed. Two accounts sharing an email would make
"which account does this Google identity mean?" ambiguous, and the database is
the right place to make that unrepresentable rather than a check the service
layer could race past.

Revision ID: 0011_google_sign_in
Revises: 0010_bounded_failure_retry
Create Date: 2026-08-08
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0011_google_sign_in"
down_revision: str | None = "0010_bounded_failure_retry"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # 320 = the practical maximum length of an email address (64 local + @ + 255
    # domain). Stored lowercased by the service layer so lookups match without a
    # citext extension.
    op.add_column("users", sa.Column("email", sa.String(320), nullable=True))
    # Google's `sub` claim: an opaque, immutable, per-account identifier. Bound
    # on first successful sign-in and matched ahead of email thereafter.
    op.add_column("users", sa.Column("google_sub", sa.String(255), nullable=True))

    # Unique but nullable: Postgres treats NULLs as distinct in a unique index,
    # so every existing password-only account coexists happily with NULL in both
    # columns while two real values can never collide.
    op.create_index("ix_users_email", "users", ["email"], unique=True)
    op.create_index("ix_users_google_sub", "users", ["google_sub"], unique=True)

    op.alter_column(
        "users", "password_hash", existing_type=sa.String(256), nullable=True
    )


def downgrade() -> None:
    # Restoring NOT NULL would fail on any Google-only account, which by
    # definition has no password to put back. Such a row cannot be downgraded
    # without either inventing a credential or silently deleting the account, so
    # this refuses instead of guessing. Delete or give those accounts a password
    # first, then downgrade.
    bind = op.get_bind()
    passwordless = bind.execute(
        sa.text("SELECT count(*) FROM users WHERE password_hash IS NULL")
    ).scalar_one()
    if passwordless:
        raise RuntimeError(
            f"{passwordless} account(s) have no password and would lose their only "
            "way to sign in. Set a password on them (scripts/create_user.py "
            "--update) or delete them, then downgrade."
        )

    op.alter_column(
        "users", "password_hash", existing_type=sa.String(256), nullable=False
    )
    op.drop_index("ix_users_google_sub", table_name="users")
    op.drop_index("ix_users_email", table_name="users")
    op.drop_column("users", "google_sub")
    op.drop_column("users", "email")
