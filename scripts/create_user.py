"""Create or update a human operator account (ADR-012).

Why this is a script and not a migration or an API endpoint
-----------------------------------------------------------
A migration that seeded a default account would bake a credential into the
schema, and every deployment would share it. A public self-registration
endpoint would let anyone who can reach the orchestrator grant themselves the
ability to run containers on other people's laptops. Bootstrap is therefore an
explicit action taken by whoever already has shell access to the orchestrator
host — which is the only party that could meaningfully authorize it.

The password is read from an interactive prompt (with confirmation) or from
``ORCH_USER_PASSWORD``. It is deliberately **not** accepted as a command-line
argument: argv lands in shell history and is world-readable in the process
table on Linux, so a ``--password`` flag would leak the credential to every
other user on the box.

Usage
-----
First admin, interactive::

    python -m scripts.create_user --username abhishek --role ADMIN

A classmate who submits jobs but does not administer the fleet::

    python -m scripts.create_user --username priya --role OPERATOR

Non-interactive (CI, container entrypoint)::

    ORCH_USER_PASSWORD='...' python -m scripts.create_user \
        --username ci --role OPERATOR --no-prompt

Reset a forgotten password on an existing account::

    python -m scripts.create_user --username abhishek --role ADMIN --update

Google sign-in (ADR-012 addendum)
---------------------------------
``POST /auth/google`` never creates accounts, so a Google identity can only sign
in to a row that already carries its address. Setting that address is this
script's job too.

Give an existing password account the option of Google sign-in::

    python -m scripts.create_user --username abhishek --role ADMIN \
        --email abhishek@example.com --update

A classmate who only ever uses Google, with no password at all::

    python -m scripts.create_user --username priya --role OPERATOR \
        --email priya@example.com --google-only

The email is the *introduction*; Google's immutable ``sub`` is bound on the first
successful sign-in and matched ahead of the address afterwards. Changing
``--email`` on an existing account therefore clears that binding, so the next
sign-in re-binds from the new address — otherwise reassigning a school address
would hand the new holder the old holder's fleet access.

Requires ``DATABASE_URL`` to point at the orchestrator database (the same
value the server uses).
"""

from __future__ import annotations

import argparse
import asyncio
import getpass
import os
import sys

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import async_sessionmaker

from orchestrator.core.config import get_settings
from orchestrator.core.db import get_engine
from orchestrator.core.security import hash_password
from orchestrator.models.user import UserRole
from orchestrator.services.users import (
    MIN_PASSWORD_LENGTH,
    NoCredentialError,
    UserExistsError,
    WeakPasswordError,
    create_user,
    get_user_by_username,
    normalize_email,
)

_PASSWORD_ENV = "ORCH_USER_PASSWORD"


def _read_password(*, prompt: bool) -> str:
    """Get the password from the environment, or prompt for it twice.

    Never from argv — see the module docstring.
    """
    from_env = os.environ.get(_PASSWORD_ENV)
    if from_env:
        return from_env
    if not prompt:
        raise SystemExit(
            f"error: --no-prompt requires {_PASSWORD_ENV} to be set in the environment."
        )
    if not sys.stdin.isatty():
        raise SystemExit(
            f"error: stdin is not a terminal, so no password can be prompted for. "
            f"Set {_PASSWORD_ENV} instead."
        )
    first = getpass.getpass("Password: ")
    second = getpass.getpass("Confirm password: ")
    if first != second:
        raise SystemExit("error: passwords did not match.")
    return first


async def _run(
    *,
    username: str,
    role: UserRole,
    password: str | None,
    update: bool,
    email: str | None,
) -> int:
    settings = get_settings()
    engine = get_engine(settings)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)

    async with session_factory() as session:
        existing = await get_user_by_username(session, username=username)

        if existing is not None:
            if not update:
                print(
                    f"User {username!r} already exists (role={existing.role.value}). "
                    f"Pass --update to reset its password and role.",
                    file=sys.stderr,
                )
                return 1
            if password is not None and len(password) < MIN_PASSWORD_LENGTH:
                print(
                    f"error: password must be at least {MIN_PASSWORD_LENGTH} "
                    f"characters.",
                    file=sys.stderr,
                )
                return 2
            if password is not None:
                existing.password_hash = hash_password(password)
            if email is not None:
                # Changing the email moves Google sign-in to a different address,
                # so the previously bound Google account must not keep access
                # under the old binding. Clearing google_sub makes the next
                # sign-in re-bind from the new address (ADR-012 addendum §4).
                new_email = normalize_email(email)
                if existing.email != new_email:
                    existing.email = new_email
                    existing.google_sub = None
            existing.role = role
            try:
                await session.commit()
            except IntegrityError:
                await session.rollback()
                print(
                    f"error: email {email!r} is already on another account.",
                    file=sys.stderr,
                )
                return 1
            changed = ", ".join(
                filter(
                    None,
                    [
                        "password" if password is not None else None,
                        "email" if email is not None else None,
                        f"role={role.value}",
                    ],
                )
            )
            print(f"Updated user {username!r} ({changed}).")
            return 0

        try:
            user = await create_user(
                session,
                username=username,
                password=password,
                role=role,
                email=email,
            )
        except WeakPasswordError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2
        except NoCredentialError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2
        except UserExistsError as exc:
            # Lost a race with a concurrent create between the lookup above and
            # the insert. The unique index is what actually guarantees this.
            print(f"error: {exc}", file=sys.stderr)
            return 1
        await session.commit()
        how = "Google only" if password is None else "password"
        print(
            f"Created user {user.username!r} (role={user.role.value}, "
            f"sign-in={how}, email={user.email or '-'}, id={user.id})."
        )
        return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Create or update a human operator account.",
        epilog=(
            f"The password comes from the {_PASSWORD_ENV} environment variable "
            "or an interactive prompt. It is never accepted on the command line."
        ),
    )
    parser.add_argument("--username", required=True, help="Unique login name.")
    parser.add_argument(
        "--role",
        required=True,
        choices=[r.value for r in UserRole],
        help="ADMIN administers the fleet (enrollment tokens); OPERATOR submits "
        "and reads jobs.",
    )
    parser.add_argument(
        "--update",
        action="store_true",
        help="Reset the password and role of an existing account.",
    )
    parser.add_argument(
        "--no-prompt",
        action="store_true",
        help=f"Fail instead of prompting; requires {_PASSWORD_ENV}.",
    )
    parser.add_argument(
        "--email",
        default=None,
        help="Verified email that Google sign-in matches this account by. "
        "Required with --google-only. Changing it unbinds any Google account "
        "previously linked, so the next sign-in re-binds from the new address.",
    )
    parser.add_argument(
        "--google-only",
        action="store_true",
        help="Create the account with no password: it signs in with Google "
        "alone. Requires --email.",
    )
    args = parser.parse_args(argv)

    if args.google_only and not args.email:
        parser.error("--google-only requires --email (there would be no way to sign in)")

    # A Google-only account has no password to read, so do not prompt for one —
    # prompting and discarding it would teach the operator the wrong model of
    # what this account is.
    password = None if args.google_only else _read_password(prompt=not args.no_prompt)

    return asyncio.run(
        _run(
            username=args.username,
            role=UserRole(args.role),
            password=password,
            update=args.update,
            email=args.email,
        )
    )


if __name__ == "__main__":
    raise SystemExit(main())
