# ADR-012 addendum: Google sign-in

## Status
Accepted. Amends ADR-012, which explicitly rejected this.

## Why this reverses a recorded decision

ADR-012's "Alternatives considered" ruled out an external IdP:

> **OIDC / external IdP (Google, GitHub).** Correct for a real product, wrong
> here. It adds a mandatory external dependency and a callback URL to a system
> whose whole premise is peers on laptops behind NATs, and it makes the
> orchestrator unusable offline. Revisit if this ever leaves the lab.

That reasoning was sound and both objections were real. This addendum exists
because the objections turn out to be avoidable rather than inherent — not
because the original judgement was wrong. Recording the reversal here, rather
than quietly editing ADR-012, is the point: the earlier decision was correct
about the *authorization-code redirect flow* it was imagining.

Two things changed the calculation:

1. **The callback URL is avoidable.** Google Identity Services can hand the
   *browser* a signed ID token directly, with no redirect URI to register and no
   authorization code to exchange. Nothing needs a stable public hostname, which
   was the part that did not fit a Tailscale address.
2. **The dependency is avoidable.** Password sign-in is untouched and always
   available. Google is strictly additive, and an orchestrator with no internet
   still works exactly as it did.

What remains true is that the flow needs *outbound* internet on the orchestrator
to fetch Google's signing keys. That cost is real and is stated in §6 rather than
hidden.

## Decision

### 1. ID-token flow, not the authorization-code redirect flow

The browser gets an ID token from Google, posts it to `POST /auth/google`, and
the orchestrator verifies it against Google's published JWKS.

The consequence worth naming: **there is no client secret anywhere in this
system.** The authorization-code flow would require one on the server, and the
temptation to put its sibling in the dashboard bundle is exactly the mistake
ADR-012 §6 was written to undo. A flow with no secret to leak cannot repeat it.
The OAuth *client ID* is public by design and is served from
`GET /auth/providers`.

Serving the client ID from the API rather than inlining it as a `VITE_` variable
is deliberate: it means enabling Google sign-in is a configuration change, not a
dashboard rebuild, and there is one source of truth for whether it is on.

### 2. Verification is exhaustive and the audience check is the load-bearing one

`services/google_oidc.py` checks the RS256 signature against Google's JWKS, the
issuer, expiry, freshness, `email_verified`, and — critically — that `aud`
equals *our* client ID.

That last check deserves emphasis because getting it wrong is silent. Google
signs the ID tokens of every one of its clients with the same keys. A token
minted for an unrelated application has a perfectly valid signature and a real
Google issuer. Without the audience check, anyone could register their own Google
client, sign in to it, and replay the resulting token here. The check is
delegated to PyJWT's `audience=` parameter rather than compared after decoding,
so it cannot be forgotten and cannot be fooled by a list-valued `aud`.

Only `RS256` is accepted. Passing an algorithm allowlist is what makes an
`alg: HS256` token signed with the (public) client ID fail instead of verifying.

### 3. Google authenticates; it does not authorize

`POST /auth/google` issues exactly the same `aud="user"` JWT that
`POST /auth/login` issues — same TTL, same `role` claim, same everything.
Nothing downstream knows which mechanism was used.

This keeps ADR-012 §3's audience separation intact and means Google sign-in adds
no second authorization path that could drift out of step with the first. There
is a test asserting the issued token is an ordinary user token.

### 4. No self-provisioning, and `sub` is the identity

**A verified Google identity that matches no account is refused.** It does not
create one, and there is no domain allowlist that would auto-create one either.
This is the property that lets an external IdP in without weakening ADR-012:
on this system an account is permission to run containers on other people's
machines, and Google can attest who someone is but cannot grant that.

An admin sets `users.email` in advance (`scripts/create_user.py --email`). Then:

- Matching is by `google_sub` when the account has one.
- Otherwise by email, and a successful sign-in **binds** `google_sub` to the row
  (trust on first use).
- An email whose account is already bound to a *different* `google_sub` is
  refused rather than rebound.

Email is the introduction; `sub` is the identity. The reason is that a Workspace
or school address can be reassigned to a different human after the original
holder leaves, while `sub` cannot. If email stayed the standing match, whoever
inherits `priya@college.edu` would inherit Priya's fleet access. Refusing the
rebind is what makes that takeover require a deliberate admin action, since a
rebind and a takeover are indistinguishable from here.

Changing `--email` on an existing account clears `google_sub` for the same
reason, so the next sign-in re-binds from the new address.

### 5. Refusals are uniform before verification, specific after it

The dividing line is whether Google has vouched for the caller yet.

**A token that does not verify** — wrong audience, wrong issuer, expired, stale,
unverified email, bad signature — returns one identical 401. The caller has
proved nothing, so naming the failed check would only teach whoever is probing
how verification works. A test asserts three distinct causes read identically.

**An account refusal** — no account, disabled, subject mismatch — names the
address and the situation.

This second half reverses how this endpoint first shipped, and the reversal is
worth recording because the original was a reasoned mistake rather than an
oversight. The flat "Google sign-in was refused, ask whoever runs the fleet to
add your address" was borrowed from `POST /auth/login`, where it is correct: an
attacker there types any username they like, so a specific error is an
enumeration oracle.

That reasoning does not transfer. To reach the account-matching branch at all, a
caller must present a Google ID token for the address, which means controlling
that Google account. They can therefore only ever probe *their own* address, and
learn nothing they could not learn by simply signing in. There is no oracle, so
the redaction bought nothing — and it cost two concrete things:

- the operator had no way to know which address to add, even though the error
  told the user to go and ask for exactly that;
- the user could not tell a missing account from a disabled one, which are
  different problems with different fixes.

The message still echoes only the address in the presented token. A subject
mismatch does not disclose the incumbent Google subject or the account's
username, and there is a test asserting that.

The same fix applies to the log. The refusal was logged through `extra={...}`,
which the configured formatter (`"%(asctime)s %(levelname)s %(name)s
%(message)s"`) renders not at all — so it emitted a bare `google_sign_in_refused`
with every useful field dropped. Operational facts now go in the message, which
is what the rest of the codebase does.

Google being *unreachable* remains a 503 rather than a 401, because telling
someone "invalid login" when the real problem is that the orchestrator has no
route to Google sends them hunting for a credential fault that does not exist.

### 6. Accepted limitations

Stated rather than papered over, in the manner ADR-012 §6 treated
`sessionStorage`:

- **Replay is bounded, not eliminated.** A Google ID token is a bearer credential
  for its whole ~1 hour validity and carries nothing making it single-use.
  Whoever holds one can present it here. We require `iat` to be recent
  (`GOOGLE_ID_TOKEN_MAX_AGE_SECONDS`, default 300 s), which shrinks the window
  by an order of magnitude, because a real sign-in forwards the token within
  seconds. Full protection needs a server-issued single-use nonce echoed in the
  token, which means nonce state on the login path — the machinery already exists
  for nodes (`models/nonce.py`) and this is the obvious next step if the exposure
  ever matters. It is not built today, and the gap is recorded here rather than
  implied to be closed.
- **Outbound internet is required on the orchestrator** for the JWKS fetch, and
  only for Google sign-in. Keys are cached for an hour, an unknown `kid` forces
  one refetch so a rotation self-heals, and a failed fetch degrades to a 503 on
  that endpoint alone. Password login is unaffected. An air-gapped deployment
  simply leaves `GOOGLE_OAUTH_CLIENT_ID` unset and loses nothing it had before.
- **The browser loads a Google script** (`accounts.google.com/gsi/client`), which
  is a third-party origin the dashboard did not previously contact. It is loaded
  on demand, so a deployment with Google sign-in disabled never contacts Google.
- **A stolen token is still valid until it expires.** Unchanged from ADR-012;
  the same TTL-bounded revocation trade, accepted for the same reason.
- **The rate limiter is still per-process.** `POST /auth/google` shares the
  `login` bucket deliberately — both are attempts to obtain a user token from
  one IP, and separate buckets would let an attacker double their budget by
  alternating endpoints. ADR-012 §7's multi-replica caveat applies unchanged.

## Alternatives considered

**Authorization-code flow with a client secret.** The textbook server-side flow.
Rejected because it needs a registered redirect URI — the specific thing ADR-012
objected to, and genuinely awkward for an orchestrator reached at a Tailscale
address — and because it introduces a client secret whose only purpose here would
be to be stored somewhere. The ID-token flow gives the same assurance for this
use case with strictly less to get wrong.

**Auto-provisioning accounts from an allowed email domain.** Tempting for a class
project: let anyone with a `@college.edu` address sign in and get `OPERATOR`.
Rejected because it converts "an admin decided this person may run code on my
laptop" into "this person attends the same institution", which is a materially
weaker claim, and because the blast radius is other people's hardware. Adding it
later is a small change to one function; it is deliberately not the default.

**Making Google the only sign-in.** Rejected outright. It would make the
orchestrator unusable offline and unbootstrappable before any account exists,
which is precisely why `scripts/create_user.py` exists at all.

**A `citext` column for email.** Rejected in favour of normalizing to lowercase
in the service layer, so no deployment needs a Postgres extension installed for
sign-in to work. Gmail's dot-insensitivity and `+tag` suffixes are deliberately
*not* normalized: collapsing them would silently merge addresses an admin entered
as distinct.

## Consequences

- `users` gains `email` and `google_sub` (both uniquely indexed, both nullable),
  and `password_hash` becomes nullable so a Google-only account can exist.
  Migration `0011_google_sign_in`.
- A NULL `password_hash` is a *refusal* of the password path, never a wildcard.
  `authenticate_user` still spends the KDF on such an account so timing does not
  reveal which accounts sign in with Google — those would otherwise be the ones
  worth phishing.
- No existing row is backfilled. Every current account keeps its password and
  gets NULL for both new columns, so nothing can sign in with Google until an
  admin sets an address deliberately. Guessing which Google address belongs to an
  existing username would be fabricating an identity binding.
- `downgrade()` refuses to run while any passwordless account exists, rather than
  inventing a credential or dropping the account.
- The `server` extra gains `httpx` (already pinned at the same version by the
  agent and bench extras) for the async JWKS fetch.
- `tests/test_google_auth.py` signs real RS256 tokens with a locally generated
  key and serves them through a stubbed JWKS endpoint, so the whole verification
  path is exercised without the suite depending on the internet.
