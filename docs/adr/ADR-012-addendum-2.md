# ADR-012 addendum 2: admin user management over the API

## Status
Accepted. Extends ADR-012. Does **not** change Google sign-in (ADR-012
addendum), which continues to work exactly as specified there.

## Context

ADR-012 made `scripts/create_user.py` the way accounts come into being, and gave
a good reason:

> Bootstrap is therefore an explicit action taken by whoever already has shell
> access to the orchestrator host — which is the only party that could
> meaningfully authorize it.

That is right for the *first* account. It was quietly wrong for every account
after it, and Google sign-in made the cost obvious. The flow a classmate hit was:

1. They click **Sign in with Google**.
2. They are refused, because no account carries their address — correctly, since
   the addendum deliberately does not self-provision.
3. An admin must now SSH to the orchestrator host, export `DATABASE_URL`, and run
   `scripts/create_user.py --email ... --update`.

Step 3 is the entire friction. It also means the person who can add a machine to
the fleet through the dashboard cannot add a *person* without a terminal.

## Decision

### 1. Admin-only CRUD at `/users`

`GET /users`, `POST /users`, `PATCH /users/{id}`, all behind `require_admin_user`,
declared on the router rather than per-route so a route added later is gated by
default.

The privilege argument that justified requiring SSH no longer holds. An ADMIN can
already mint enrollment tokens (ADR-012 §6) and upload datasets executed on other
people's machines (ADR-014). "Can add a person" is not a larger privilege than
either; requiring a shell for it protected nothing and cost every invitation a
terminal session.

**There is still no self-registration.** An admin creates accounts; nobody
creates their own. That is the property ADR-012 actually cared about, and it is
untouched.

`scripts/create_user.py` stays. It remains the bootstrap for the first admin,
before any account exists to authenticate as, and it is still the only way in if
the dashboard is unreachable.

### 2. No DELETE — disable instead

Accounts are disabled, never deleted, reusing the `disabled_at` column ADR-012
already defined for exactly this reason: a deleted user would leave the jobs they
submitted with an unresolvable author. Disabling takes effect immediately,
because `require_user` re-reads the row rather than trusting the token's claims.

### 3. The last-admin guard

An update that would leave **zero enabled ADMIN accounts** is refused with 409.

This is not a courtesy check. Every administrative action requires ADMIN, so a
fleet with no enabled admin can only be repaired from a shell on the orchestrator
host — precisely the situation this addendum exists to remove. Without the guard
it is reachable in two clicks by an admin demoting themselves.

The guard evaluates the *resulting* state rather than one field at a time, so
`{"role": "OPERATOR", "disabled": true}` in a single request is caught the same
way either field alone would be. A disabled admin does not count toward the
total: an admin nobody can sign in as is not an admin.

409 rather than 403 is deliberate — the caller has the right to perform this
operation in general, and it is the current state of the fleet that forbids it
now. The message says what to do about it.

### 4. PATCH, because absent and null must differ for `email`

`email: null` clears the address and with it Google sign-in. Omitting `email`
leaves it alone. Both arrive as `None` in Python, so the handler checks
`model_dump(exclude_unset=True)` for the key's presence.

Conflating them would silently revoke someone's Google sign-in every time an
admin changed only their role — a bug that would look like Google being flaky
rather than like an API design error. There is a test for each direction.

Changing an email still clears `google_sub`, exactly as
`scripts/create_user.py --update` does, so a reassigned address cannot inherit
the previous holder's access (ADR-012 addendum §4). That rule now lives in
`services/users.update_user` and both callers go through it.

### 5. Passwords over the API

`POST /users` and `PATCH` accept a password, subject to the same
`MIN_PASSWORD_LENGTH` floor as the script.

This is a new place a plaintext password crosses the wire, and it is worth being
explicit that it is *not* a new exposure class: `POST /auth/login` already carries
one over the same transport, which ADR-010 assumes is a Tailscale overlay.
Anything outside that overlay needs TLS in front, which `docs/STATUS.md` §2
already records as an open gap. The `argv` reasoning that keeps the script from
taking `--password` does not apply to a request body — argv is world-readable in
`/proc`; an HTTPS body is not.

Responses never carry `password_hash`. `has_password` is derived from whether the
hash is null, so an admin can tell a Google-only account from a password one
without the hash ever leaving the database.

## Alternatives considered

**A self-service "request access" flow.** Rejected. It reintroduces exactly the
self-registration ADR-012 forbade, and the approval queue it needs is more
machinery than a two-field form solves.

**Letting a Google sign-in auto-provision after admin approval.** Rejected for
the same reason ADR-014 rejected domain-based provisioning: it converts "an admin
decided this person may run code on my laptop" into "this person has an address",
and the blast radius is other people's hardware.

**Per-user ownership so admins can only manage accounts they created.** Rejected
as modelling a hierarchy that exists nowhere else in this system — there are two
roles and no notion of "my" resources (ADR-012 §2, ADR-014).

## Consequences

- No schema change. Every column these endpoints touch — `email`, `role`,
  `disabled_at`, `password_hash` — already exists.
- `scripts/create_user.py` and the API now share `update_user`, so the
  email-unbinds-Google rule cannot drift between them.
- The dashboard gains a **People** page, hidden from non-admins in the sidebar.
  That is cosmetic only: the server re-checks the role, so a tampered client can
  reveal the page and still do nothing with it.
- Inviting someone who will use Google is now one field: set their address, and
  their first sign-in binds to it. The refusal message added in the previous
  addendum names the address that was refused, so an admin can copy it straight
  into this form.
- **Accepted limitation: no audit log of administrative actions.** Creating,
  disabling, and role changes are written to the application log with the acting
  admin's username, but there is no queryable trail the way `job_events` records
  job history. If accounts ever become contentious, that is the gap to close.
