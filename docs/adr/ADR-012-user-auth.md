# ADR-012: Human (user) authentication and authorization

## Status
Accepted

## Context

ADR-008 gave *machines* an identity: a peer node enrolls with a one-time
token, proves possession of its own Ed25519 key, and carries a short-lived
JWT with `aud="node"`. That layer is done and works.

Humans have no identity at all. Through M7 the entire operator surface is
open to anyone who can reach the orchestrator's port:

| Endpoint | Gate before M8 |
| --- | --- |
| `POST /jobs` | **none** |
| `POST /jobs/{id}/cancel` | **none** |
| `GET /jobs`, `GET /jobs/{id}`, `/metrics`, `/logs`, `/scheduling-decisions` | **none** |
| `GET /nodes`, `GET /nodes/{id}` | **none** |
| `POST /auth/enrollment-tokens` | static `ADMIN_API_KEY` header |

This is not a theoretical exposure. Under ADR-010's target topology the
orchestrator is reachable by every peer on the Tailscale overlay, and
`POST /jobs` is remote code execution by design — it causes a container to
start on somebody else's laptop and consume their GPU. An unauthenticated
`POST /jobs/{id}/cancel` lets any peer destroy any other peer's run.

Worse, the one gate that does exist leaks. The dashboard mints enrollment
tokens using `VITE_ADMIN_API_KEY`, and every `VITE_`-prefixed variable is
inlined into the built JavaScript at build time. The admin key — which
gates the ability to enroll new nodes into the fleet — is readable by
anyone who opens devtools on the dashboard. A secret in a browser bundle
is not a secret.

## Decision

### 1. Real user accounts, not a second shared key

A `users` table with `username`, `password_hash`, and a `role` of `ADMIN`
or `OPERATOR`. `POST /auth/login` exchanges a password for a short-lived
JWT with `aud="user"` and a `role` claim.

The tempting cheap alternative — a second static shared key, "operator key"
instead of "admin key" — is rejected. A shared key cannot be revoked for
one person without rotating it for everyone, produces no attributable
audit trail (`submitted_by` becomes an unverified free-text claim), and
would land us right back in the browser-bundle problem the moment the
dashboard needs it. Distinct accounts cost one table and buy per-person
revocation and a real actor identity on every job.

### 2. Two roles, because there are exactly two privilege levels

- `OPERATOR` — submit, cancel, and read jobs and nodes. What a classmate
  contributing a laptop and running training needs.
- `ADMIN` — everything an operator can do, plus fleet administration:
  minting and revoking enrollment tokens.

No finer-grained permission model. There is no third real privilege level
in this system, and inventing one would be modelling a hierarchy that does
not exist.

### 3. Audience separation is enforced, both directions

A node token must not act as a human, and a human token must not act as a
node. Both are HS256 JWTs signed with the same `JWT_SIGNING_KEY`, so the
`aud` claim is the only thing standing between them — which means it must
be verified, not merely present.

`decode_node_jwt` requires `aud="node"`; `decode_user_jwt` requires
`aud="user"`. Neither accepts the other, and this has explicit negative
tests in both directions (`tests/test_auth_scope_negative.py`). Without
that check a node's own token — which every peer legitimately holds —
would be a valid operator credential, and the entire user layer would be
decorative.

### 4. `submitted_by` comes from the token, not the request body

Before M8 the client asserted who it was. `JobSubmitRequest.submitted_by`
is now ignored in favour of the authenticated username. Attribution that
the actor can set to any string it likes is not attribution.

### 5. Password hashing: `hashlib.scrypt` from the standard library

scrypt is a memory-hard KDF, it is in Python's standard library, and it
needs no new dependency. Parameters `n=2**15, r=8, p=1` (~32 MB per hash),
with a 16-byte random salt, stored as
`scrypt$<n>$<r>$<p>$<salt_b64>$<hash_b64>` so the cost parameters travel
with the hash and can be raised later without invalidating existing rows.

Argon2id would be marginally preferable on cryptographic merit, but it
means adding `argon2-cffi` and a native build to every install path. scrypt
at these parameters is a proper password KDF and is not the weak link here.

The single-purpose SHA-256 in `hash_token()` is *not* changed and is not a
mistake: enrollment tokens are 256 bits of `secrets.token_urlsafe` entropy.
A slow KDF defends against guessing a low-entropy human-chosen secret;
there is nothing to guess in a full-entropy random token.

### 6. The browser holds a user token, never an admin key

`VITE_ADMIN_API_KEY` is deleted outright — not moved, not obscured. The
dashboard logs in like a person and holds a short-lived user JWT in
`sessionStorage`.

`sessionStorage` over an httpOnly cookie is a deliberate trade-off. The
dashboard is a separate origin from the API, and a cross-origin httpOnly
cookie requires `SameSite=None; Secure`, which requires HTTPS — which
ADR-010's Tailscale topology does not terminate. A bearer token in
`sessionStorage` is readable by XSS; an httpOnly cookie is not, but is
exposed to CSRF and does not work at all over the transport we actually
have. Given the dashboard is a static SPA that renders no user-supplied
HTML, and that Tailscale already provides WireGuard transport encryption,
the bearer token is the honest choice here. If this ever runs behind real
TLS, revisiting this is the first thing to do.

The static `ADMIN_API_KEY` survives for exactly one purpose: out-of-band
CLI bootstrap before any user exists. `POST /auth/enrollment-tokens`
accepts *either* the admin key or an `ADMIN`-role user token.

### 7. Rate limiting on credential endpoints

A fixed-window counter per (client IP, endpoint class) on `/auth/login`,
`/auth/challenge`, `/auth/token/refresh`, and `/nodes/register` — the four
places where an attacker can guess a secret. Over the limit returns 429
with `Retry-After`.

This is in-process state, not shared. That is a real limitation and it is
documented rather than papered over: with more than one orchestrator
replica the effective limit multiplies by the replica count. ADR-010
deploys exactly one, so it is correct for the topology we have, and the
fix when that changes is a shared store, not a bigger number.

## Alternatives considered

**OIDC / external IdP (Google, GitHub).** Correct for a real product,
wrong here. It adds a mandatory external dependency and a callback URL to
a system whose whole premise is peers on laptops behind NATs, and it makes
the orchestrator unusable offline. Revisit if this ever leaves the lab.

**Signed job specs.** The original hardening list included cryptographically
signing job specs so an agent could verify a spec's provenance. On
examination this buys nothing here, so it is deliberately **not** built:

- It does not stop a malicious *submitter* — the orchestrator would
  faithfully sign the hostile spec. That threat is handled by strict
  validation at the boundary (`JobSpec` is `extra="forbid"` with a
  `Literal` dataset allowlist and bounded numerics), which already exists.
- It does not stop a compromised *orchestrator* — the same component that
  would be compromised holds the signing key.
- It only defends against in-transit tampering, and the right control for
  that is transport security (Tailscale's WireGuard today, TLS later), not
  an app-layer signature over one field.

Building it anyway would add key management and a verification path that
protects against nothing this system actually faces. Recorded here so the
omission is a decision with a reason, not an oversight.

**mTLS for humans.** ADR-008 already keeps mTLS as an optional node
profile. Extending it to humans means issuing and installing client
certificates in browsers, which is a materially worse experience than a
password for zero gain over a short-lived JWT.

## Consequences

- There is no anonymous access to anything but `/health`, `/metrics`, and
  the installer endpoints. A fresh deployment is unusable until an admin
  user exists, which is why `scripts/create_user.py` is part of the
  documented bootstrap (M10 handover) rather than an afterthought.
- Job attribution is now trustworthy, so the dashboard's "submitted by"
  column means something. Jobs submitted before M8 keep whatever string
  the client sent; they are not retroactively relabelled, because we do
  not know who submitted them and guessing would be fabrication.
- A stolen user token is valid until it expires (15 min default). There is
  no revocation list. Bounded-by-TTL revocation is the same trade ADR-008
  already made for nodes, accepted for the same reason.
- Every existing test that called a job or node endpoint now needs a
  credential. That churn is the point: it proves the gate is actually on
  every route rather than on a representative sample.
- The `users` table stores password hashes. `scripts/create_user.py` reads
  the password from a prompt or `ORCH_USER_PASSWORD`, never from `argv`,
  so it does not land in shell history or the process table.
