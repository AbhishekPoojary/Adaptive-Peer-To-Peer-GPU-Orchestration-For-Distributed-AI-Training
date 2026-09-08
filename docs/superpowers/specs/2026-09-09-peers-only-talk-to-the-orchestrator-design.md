# Peers only ever talk to the orchestrator

**Date:** 2026-09-09
**Status:** approved, not yet implemented

## The goal, in the user's words

> "Anyone from any corner of the world can connect with another system and train
> their dataset."
>
> "The person must easily upload a dataset and should get the model back without
> tinkering much things."

## The problem

A peer today needs a working route to **two** services, and needs credentials
for one of them:

| What the peer needs | Why | What breaks |
|---|---|---|
| The orchestrator | enrol, heartbeat, claim leases | nothing — this part works |
| MinIO, directly | download the dataset, upload the model | everything below |
| MinIO's root credentials | the trainer writes checkpoints itself | cannot be given to strangers |

The dataset URL is presigned against `S3_ENDPOINT_URL`, and **a signature covers
the host it was signed for** — it cannot be rewritten afterwards. So that value
must already name a host the peer can resolve. The compose default
(`http://minio:9000`) resolves only inside the compose network, so a peer
training on its own machine fails with

```
URLError: <urlopen error [Errno 11001] getaddrinfo failed>
```

before the first byte. Pinning a LAN IP there works until the IP changes, which
during one afternoon of development happened three times.

Checkpointing has the mirror-image problem. `trainer/checkpoint.py` writes
straight to S3, so the peer needs `S3_ACCESS_KEY` / `S3_SECRET_KEY`. The
installer passes neither, which is why runs log

```
checkpoint: S3/MinIO not configured; running without checkpoint/resume
```

and why the job page has no model to hand back. Fixing that by shipping
credentials to peers would mean granting object-store write access to anyone
holding an enrolment token — unacceptable once peers are strangers.

## The principle

**A peer talks to exactly one host: the orchestrator.** Object storage is an
implementation detail behind it. This yields, in one stroke:

- one port to expose instead of two,
- no storage credentials anywhere outside the orchestrator,
- no `S3_ENDPOINT_URL` for an operator to get wrong,
- and a system that works unchanged over a LAN, a VPN, or a public tunnel,
  because the orchestrator derives its own address from each request's `Host`
  header — the technique `orchestrator/api/installer.py` already uses to bake
  the right URL into the served install script.

## Piece 1 — the orchestrator serves datasets

**New endpoint.** `GET /datasets/{dataset_id}/archive?token=…` streams the
archive from object storage. Chunked, so a 2 GiB upload is never held in
memory — the same shape as the existing `/jobs/{id}/checkpoint/download`.

**Authentication.** The trainer fetches with a bare
`urllib.request.urlopen(url)` and sends no headers, so the URL must carry its
own proof. A short-lived JWT in the query string, `aud="dataset"`,
`sub=<dataset_id>`, signed with `jwt_signing_key`. This extends the existing
audience separation (`aud="user"` vs `aud="node"`) rather than inventing a
second mechanism, and a token for one dataset cannot read another.

**Where the URL comes from.** `_attach_dataset_fetch` in
`orchestrator/api/leases.py` stops presigning and instead builds
`{public_base}/datasets/{id}/archive?token=…`, where `public_base` is derived
from the claiming request's `X-Forwarded-Host` / `Host`. The peer therefore
receives an address it demonstrably reached moments earlier.

`claim_lease` does not currently receive the request object, so it gains a
`request: Request` parameter and passes it through — a signature change, not
a behavioural one. The header-parsing helper already exists in
`orchestrator/api/installer.py` and should be lifted to a shared module
rather than duplicated, since two callers will then need it.

**The trainer does not change.** It still calls `urlopen` on whatever
`dataset_url` holds, and still verifies the SHA-256 before extracting, so the
integrity guarantee is unchanged.

## Piece 2 — the model comes back through the orchestrator

`trainer/checkpoint.py` already defines a two-method `ObjectStore` Protocol:

```python
def put_bytes(self, key: str, data: bytes) -> None: ...
def get_bytes(self, key: str) -> bytes: ...
```

So this is a new implementation of an existing seam, not a rewrite.

**New store.** `HttpObjectStore` implements the same Protocol against the
orchestrator:

- `PUT  {base}/jobs/{job_id}/checkpoint-blobs/{key}`
- `GET  {base}/jobs/{job_id}/checkpoint-blobs/{key}`

**Selection.** `S3ObjectStore.from_env` keeps working, so a co-located dev
setup is unchanged. A new `store_from_env` prefers `HttpObjectStore` when
`CHECKPOINT_UPLOAD_URL` and `CHECKPOINT_UPLOAD_TOKEN` are present, falls back
to S3, then to `None` (no checkpointing) exactly as today.

**Authentication.** A job-scoped JWT, `aud="checkpoint"`, `sub=<job_id>`,
minted at claim time alongside the dataset token and passed to the trainer by
both launchers (`agent/runtime/docker_launcher.py` and
`subprocess_launcher.py`) as environment variables.

**Authorisation.** The endpoint rejects any key not under that job's own
prefix. `manifest_key(job_id)` and `blob_key(job_id, …)` both embed the job id,
so this is a prefix check, and a token for one job cannot touch another's
blobs — nor anything else in the bucket.

**Token lifetime.** Scoped to a single job's checkpoint prefix and configurable
via `checkpoint_token_ttl_seconds`, defaulting to 24 h. Long-lived by JWT
standards, deliberately: a training run outlives the 15-minute access-token TTL
and the trainer is a subprocess whose environment is fixed at launch, so it
cannot refresh. The blast radius is one job's checkpoint objects.

**Credentials stop travelling.** With this in place the peer never receives
`S3_ACCESS_KEY` or `S3_SECRET_KEY`, and the "hand out MinIO credentials"
alternative is dropped.

## Piece 3 — the session stops expiring mid-upload

`user_access_token_ttl_seconds` is 900 and there is no user-facing refresh:
`/auth/token/refresh` is the node challenge-response path, which needs an
Ed25519 key a browser does not have. A user who idles fifteen minutes — say,
fetching a dataset off another machine — gets `invalid or expired token` on
their next action, and the page's other queries fail behind it.

**Change.** A `POST /auth/token/renew` that accepts a valid, unexpired user
token and returns a fresh one, plus a dashboard timer that renews at roughly
half the TTL. Renewal from an already-valid token only: an expired token still
requires signing in, so this shortens no security property, it only stops a
live session dying between two clicks.

## Piece 4 — reachable from anywhere

Pieces 1 and 2 reduce the public surface to a single HTTP port, which makes
this tractable with no server and no domain.

**Now — a Cloudflare quick tunnel.**

```
cloudflared tunnel --url http://localhost:5173
→ https://<random>.trycloudflare.com
```

No account, no domain, no VPS, real TLS. A collaborator anywhere opens that
URL, signs in, uploads a dataset, and downloads the model, having installed
nothing. The dashboard's own dev-server proxy forwards `/api` to the
orchestrator server-side, so only one tunnel is needed.

Stated plainly, because a demo should not rest on an unstated assumption: quick
tunnels are **ephemeral** (a new hostname each restart) and rate-limited. Fine
to demonstrate with, not a deployment.

`VITE_ALLOWED_HOSTS` must include the tunnel hostname — Vite rejects unknown
`Host` headers, which is the first thing that will appear to be a tunnel
failure and is not.

**Later — a real deployment.** A compose overlay plus a Caddy site file that
terminates TLS via Let's Encrypt and serves the *built* dashboard as static
files rather than the dev server. `vite.config.ts` already says this in as many
words: *"For anything long-lived, build the dashboard and serve the static
output behind a real web server instead."* Written now, unused until there is a
domain to point at it.

**Multi-node stays a separate problem.** For `world_size > 1`, every rank dials
the rendezvous host at `RENDEZVOUS_PORT`, so cohorts need genuine
peer-to-peer reachability that no reverse tunnel provides. Tailscale is the
answer there. `world_size = 1` — the demo case, and the common case — works
over a plain tunnel. This spec does not claim to solve cohorts.

## What this does not fix

- **No TLS between the orchestrator and a peer on a LAN.** Known gap #2 in
  `docs/STATUS.md`. The tunnel provides TLS for browser traffic; a LAN peer
  still speaks plain HTTP to the orchestrator.
- **Enrolment tokens remain bearer credentials** for their TTL.
- **The trainer still runs unsandboxed** when Docker is absent, with the
  existing consent prompt.
- **`world_size > 1` across NAT.** Explicitly out of scope, see above.

## Testing

1. `dataset_url` is an orchestrator URL, never a storage URL, and its host
   tracks the claiming request's `Host` header.
2. A dataset token for dataset A cannot fetch dataset B; an expired one is
   rejected.
3. `HttpObjectStore` satisfies the `ObjectStore` Protocol — the same
   round-trip tests the S3 store passes.
4. A checkpoint token for job A cannot read or write job B's blobs, and a key
   outside the job's prefix is rejected.
5. `store_from_env` selects HTTP over S3 when both are configured, and returns
   `None` when neither is.
6. Renewing a valid user token returns a new one; an expired token is refused.
7. End to end, with `S3_ENDPOINT_URL` deliberately set to a **garbage** value:
   upload, submit, train, download the model. This is the regression test for
   the whole spec — it must pass precisely because no peer consults that
   setting any more.

## Consequences

`S3_ENDPOINT_URL` becomes orchestrator-only configuration. The
`deploy/.env.example` guidance added for peers, and the LAN-IP workaround in
`deploy/.env`, both become obsolete and should be removed when this lands —
leaving them would tell a future operator to maintain a value nothing reads.
