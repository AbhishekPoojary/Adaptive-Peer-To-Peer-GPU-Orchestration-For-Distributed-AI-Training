"""User account service (ADR-012): creation, password and Google authentication.

Authentication deliberately does the same amount of work whether or not the
username exists — see :func:`authenticate_user`.

Google sign-in (ADR-012 addendum) lands here already verified: by the time
:func:`authenticate_google_identity` is called, :mod:`google_oidc` has proved
Google asserted the identity. This module's only job is deciding whether that
identity corresponds to an account, and it never creates one.
"""

from __future__ import annotations

import enum
import uuid
from datetime import UTC, datetime

from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from orchestrator.core.security import hash_password, verify_password
from orchestrator.models.user import User, UserRole
from orchestrator.services.google_oidc import GoogleIdentity

#: A syntactically valid scrypt hash of a random throwaway password, computed
#: once at import. authenticate_user verifies the submitted password against
#: this when the username does not exist, so a miss costs the same ~50-100 ms
#: as a hit. Without it, "username not found" returns fast enough to enumerate
#: valid usernames by timing alone.
_DUMMY_HASH = hash_password(uuid.uuid4().hex)

#: Minimum password length accepted at creation. Not a strength oracle — just
#: a floor that stops a one-character password from reaching the database.
MIN_PASSWORD_LENGTH = 12


class UserExistsError(Exception):
    """Raised when the requested username or email is already taken."""


class WeakPasswordError(Exception):
    """Raised when a password fails the length floor."""


class NoCredentialError(Exception):
    """Raised when an account would be created with no way to sign in at all.

    A row with neither a password nor an email is unreachable by both auth paths.
    Creating one silently would look like success and produce an account nobody
    can use, so it is refused at the boundary.
    """


async def create_user(
    session: AsyncSession,
    *,
    username: str,
    password: str | None,
    role: UserRole,
    email: str | None = None,
) -> User:
    """Create an account. Caller commits.

    ``password`` may be ``None`` to create a Google-only account (ADR-012
    addendum), in which case ``email`` is required — otherwise the row would have
    no credential of either kind and nobody could ever sign in to it.

    Uniqueness is enforced by the database's unique indexes and the resulting
    IntegrityError is translated here, rather than by a check-then-insert that two
    concurrent creations could both pass.
    """
    normalized_email = normalize_email(email) if email is not None else None

    if password is None and normalized_email is None:
        raise NoCredentialError(
            "an account needs either a password or an email to sign in with Google"
        )
    if password is not None and len(password) < MIN_PASSWORD_LENGTH:
        raise WeakPasswordError(
            f"password must be at least {MIN_PASSWORD_LENGTH} characters"
        )

    user = User(
        username=username,
        password_hash=hash_password(password) if password is not None else None,
        email=normalized_email,
        role=role,
    )
    session.add(user)
    try:
        await session.flush()
    except IntegrityError as exc:
        await session.rollback()
        # Name the field that actually collided. "username is taken" when the
        # real clash was the email sends an admin renaming the wrong thing.
        detail = str(exc.orig) if exc.orig is not None else str(exc)
        if "ix_users_email" in detail:
            raise UserExistsError(
                f"email {normalized_email!r} is already on another account"
            ) from exc
        raise UserExistsError(f"username {username!r} is already taken") from exc
    await session.refresh(user)
    return user


async def get_user_by_username(session: AsyncSession, *, username: str) -> User | None:
    """Look up an account by its unique username."""
    result = await session.execute(select(User).where(User.username == username))
    return result.scalar_one_or_none()


async def authenticate_user(
    session: AsyncSession, *, username: str, password: str
) -> User | None:
    """Return the user iff the password matches and the account is enabled.

    Runs the KDF against a dummy hash when the username is unknown so the
    response time does not distinguish "no such user" from "wrong password".
    The caller must also return an identical error message for both.
    """
    user = await get_user_by_username(session, username=username)
    if user is None:
        verify_password(password, _DUMMY_HASH)
        return None
    if user.password_hash is None:
        # A Google-only account (ADR-012 addendum). NULL is a refusal, never a
        # wildcard: no password can satisfy it. The dummy KDF still runs so the
        # timing does not reveal that this username signs in with Google —
        # which would otherwise let a prober map the accounts worth phishing.
        verify_password(password, _DUMMY_HASH)
        return None
    if not verify_password(password, user.password_hash):
        return None
    if not user.is_active:
        # Verified the password first, deliberately: branching on the disabled
        # flag before doing the KDF work would reintroduce the timing signal.
        return None
    return user


# --- Google sign-in (ADR-012 addendum) ---------------------------------------


def normalize_email(email: str) -> str:
    """Return the form of ``email`` used for storage and lookup.

    Lowercased and stripped, so an admin who types ``Priya@Example.com`` and a
    Google token carrying ``priya@example.com`` describe the same account. The
    local part of an address is technically case-sensitive per RFC 5321, but no
    mail provider in practice treats it that way, and matching case-sensitively
    here would produce a sign-in that fails for reasons invisible to the user.

    Nothing else is normalized. Notably Gmail's dot-insensitivity and ``+tag``
    suffixes are left alone: collapsing them would silently merge addresses that
    an admin entered as distinct, and this system cannot afford to guess that two
    identities are the same person.
    """
    return email.strip().lower()


class GoogleAuthOutcome(enum.Enum):
    """Why a verified Google identity was or was not admitted.

    Separate from the HTTP layer so the reason can be logged precisely while the
    client still receives one flat message (see api.auth).
    """

    #: Matched an enabled account. The user is returned.
    OK = "OK"
    #: Google's assertion was valid, but no account carries this identity.
    #: Deliberately not a signup: an account here is permission to run
    #: containers on other people's machines.
    NO_ACCOUNT = "NO_ACCOUNT"
    #: The account exists but has been disabled.
    DISABLED = "DISABLED"
    #: The email matches an account already bound to a *different* Google
    #: subject. Refused rather than rebound — see :func:`authenticate_google_identity`.
    SUBJECT_MISMATCH = "SUBJECT_MISMATCH"


async def get_user_by_google_sub(
    session: AsyncSession, *, google_sub: str
) -> User | None:
    """Look up an account by Google's immutable subject identifier."""
    result = await session.execute(select(User).where(User.google_sub == google_sub))
    return result.scalar_one_or_none()


async def get_user_by_email(session: AsyncSession, *, email: str) -> User | None:
    """Look up an account by normalized email."""
    result = await session.execute(
        select(User).where(User.email == normalize_email(email))
    )
    return result.scalar_one_or_none()


async def authenticate_google_identity(
    session: AsyncSession, *, identity: GoogleIdentity
) -> tuple[User | None, GoogleAuthOutcome]:
    """Map a *already-verified* Google identity to an account. Caller commits.

    Matching is by ``google_sub`` first and email only as a fallback, because the
    two claims have different durability. ``sub`` is immutable for the life of
    the Google account; an email address — especially a Workspace or school one —
    can be reassigned to a different human after the original holder leaves. If
    email were the standing match, whoever inherits ``priya@college.edu`` would
    inherit Priya's fleet access.

    So email is the *introduction* and ``sub`` is the *identity*: the first
    successful sign-in binds ``sub`` to the row (trust on first use), and every
    later sign-in matches on that. An email whose account is already bound to a
    different ``sub`` is refused outright rather than rebound, because a rebind
    is indistinguishable from exactly the takeover described above.

    This function never creates an account. An unrecognized identity is a
    :attr:`GoogleAuthOutcome.NO_ACCOUNT` refusal, and that is the whole reason
    the addendum can add Google sign-in without weakening ADR-012's premise.
    """
    user = await get_user_by_google_sub(session, google_sub=identity.subject)

    if user is None:
        # First sign-in for this Google account: fall back to the email an admin
        # set in advance, and bind the subject if it fits.
        candidate = await get_user_by_email(session, email=identity.email)
        if candidate is None:
            return None, GoogleAuthOutcome.NO_ACCOUNT
        if candidate.google_sub is not None:
            # The address now belongs to someone other than whoever first signed
            # in with it. Refuse; an admin must intervene deliberately.
            return None, GoogleAuthOutcome.SUBJECT_MISMATCH
        if not candidate.is_active:
            return None, GoogleAuthOutcome.DISABLED
        candidate.google_sub = identity.subject
        user = candidate
    elif not user.is_active:
        return None, GoogleAuthOutcome.DISABLED

    return user, GoogleAuthOutcome.OK


async def record_login(session: AsyncSession, *, user_id: uuid.UUID) -> None:
    """Stamp ``last_login_at`` with the server clock. Caller commits."""
    await session.execute(
        update(User).where(User.id == user_id).values(last_login_at=func.now())
    )


# --- Admin user management (ADR-012 addendum 2) ------------------------------


class LastAdminError(Exception):
    """The change would leave the fleet with no enabled ADMIN.

    Not a courtesy guard. Every administrative action — enrolling machines,
    uploading datasets, managing accounts — requires ADMIN, so a deployment with
    zero enabled admins can only be repaired by someone with shell access to the
    orchestrator host. That is precisely the situation this addendum exists to
    stop people from ending up in, and it would be reachable in two clicks
    without this check.
    """


async def list_users(session: AsyncSession) -> list[User]:
    """Every account, newest first. Includes disabled ones.

    Disabled accounts are deliberately visible: "why can this person not sign
    in?" is the question an admin opens this list to answer, and hiding the
    answer would defeat the purpose.
    """
    result = await session.execute(select(User).order_by(User.created_at.desc()))
    return list(result.scalars().all())


async def get_user_by_id(session: AsyncSession, *, user_id: uuid.UUID) -> User | None:
    """Look up one account by id."""
    return await session.get(User, user_id)


async def count_enabled_admins(
    session: AsyncSession, *, excluding: uuid.UUID | None = None
) -> int:
    """How many enabled ADMIN accounts exist, optionally ignoring one.

    ``excluding`` answers "if this account stopped being a usable admin, how many
    would be left?" — which is the question every guard here actually asks.
    """
    statement = select(func.count()).select_from(User).where(
        User.role == UserRole.ADMIN, User.disabled_at.is_(None)
    )
    if excluding is not None:
        statement = statement.where(User.id != excluding)
    result = await session.execute(statement)
    return int(result.scalar_one())


async def update_user(
    session: AsyncSession,
    *,
    user: User,
    role: UserRole | None = None,
    password: str | None = None,
    email: str | None = None,
    email_provided: bool = False,
    disabled: bool | None = None,
) -> User:
    """Apply an admin's changes to an account. Caller commits.

    ``email_provided`` distinguishes "leave the email alone" from "clear it".
    Both arrive as ``email=None``, and conflating them would silently revoke
    someone's Google sign-in every time an admin changed only their role.

    Raises :class:`LastAdminError` if the change would remove the last enabled
    admin, and :class:`WeakPasswordError` / :class:`UserExistsError` for the same
    reasons :func:`create_user` does.
    """
    # Evaluate the lockout guard against what the account would *become*, not
    # what it is now, so demote-and-disable in one request is still caught.
    would_be_admin = (role if role is not None else user.role) is UserRole.ADMIN
    would_be_enabled = (
        not disabled if disabled is not None else user.disabled_at is None
    )
    is_usable_admin = user.role is UserRole.ADMIN and user.disabled_at is None
    stops_being_one = not (would_be_admin and would_be_enabled)
    # Short-circuit order matters: the count is a query, so it only runs for a
    # change that could actually remove an admin.
    if (
        is_usable_admin
        and stops_being_one
        and await count_enabled_admins(session, excluding=user.id) == 0
    ):
        raise LastAdminError(
            "this is the last enabled ADMIN account; promote or enable "
            "another admin first, or the fleet becomes unmanageable "
            "without shell access to the orchestrator host"
        )

    if password is not None:
        if len(password) < MIN_PASSWORD_LENGTH:
            raise WeakPasswordError(
                f"password must be at least {MIN_PASSWORD_LENGTH} characters"
            )
        user.password_hash = hash_password(password)

    if email_provided:
        new_email = normalize_email(email) if email else None
        if new_email != user.email:
            # Moving the address moves Google sign-in with it, so the binding to
            # the previous Google account must not survive — otherwise whoever
            # held the old address keeps access under the old `sub` (ADR-012
            # addendum §4).
            user.email = new_email
            user.google_sub = None

    if role is not None:
        user.role = role

    if disabled is not None:
        if disabled and user.disabled_at is None:
            user.disabled_at = datetime.now(UTC)
        elif not disabled:
            user.disabled_at = None

    # An account with neither a password nor an email cannot sign in by any
    # route. Refusing here keeps the same invariant create_user enforces, rather
    # than letting an edit reach a state creation would have rejected.
    if user.password_hash is None and user.email is None:
        raise NoCredentialError(
            "that would leave the account with no way to sign in: give it a "
            "password or an email for Google sign-in"
        )

    try:
        await session.flush()
    except IntegrityError as exc:
        await session.rollback()
        detail = str(exc.orig) if exc.orig is not None else str(exc)
        if "ix_users_email" in detail:
            raise UserExistsError(
                "that email is already on another account"
            ) from exc
        raise UserExistsError("that username is already taken") from exc
    await session.refresh(user)
    return user
