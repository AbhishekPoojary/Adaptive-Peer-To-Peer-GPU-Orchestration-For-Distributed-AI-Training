# ADR-015: Peers update themselves

## Status
Accepted

## Context
A peer's owner has one job: leave the machine on. Everything else should
happen without them. Until now, two things only changed on a peer when its
owner re-ran the install command:

* **The agent code.** The installer downloaded the bundle once and ran it
  forever. The fixes in ADR-004 addendum 2 (faster detection) and ADR-006
  addendum 3 (checkpointing that works on peers) live in the agent, so no
  existing peer would have received them.
* **The trainer image.** The installer pulled it once if it was missing, and
  never again. A rebuilt image on Docker Hub sat unused. The Linux installer
  did not even pass `--trainer-image`, so Linux peers looked for a local-only
  image they could never obtain.

## Decision

### The agent asks to be replaced; the installer replaces it
The orchestrator fingerprints the bundle it serves: a SHA-256 over file names
and contents, not over the tarball, which embeds timestamps. It ships the
fingerprint inside the bundle as `BUNDLE_VERSION` and serves it at
`GET /agent-bundle/version`. The installer passes the installed value to the
agent as `--bundle-version`.

Every 30 minutes, and **only while idle**, the agent compares the two. When they
differ it exits with code **75** (`EX_TEMPFAIL`). The installer runs the agent
in a loop. On 75 it downloads the new bundle, clears the old `agent/` and
`trainer/` source, reinstalls, and starts the agent again with the same state
directory, so it is the same node with the same reliability history.

The agent never rewrites its own files while running, so there is no
half-replaced process. A job in progress is never interrupted, because the
check is skipped while a lease is held.

If an update fails (offline, pip error), the installer restarts the agent it
already has, still passing the *old* version, so the next idle check simply
tries again. A failed update costs nothing but the attempt.

### The agent pulls the trainer image itself
At startup, and every 6 hours in a worker thread, the agent pulls the
configured trainer image when it names a registry repository (`user/name`).
A job already training keeps the image it started with; the next job uses the
new one. A failed pull keeps the local image. Locally built images with no
registry are left alone, because there is nothing to pull.

## Consequences
* **A fix reaches every peer within about 30 minutes of the orchestrator
  serving it** (6 hours for the trainer image), with no action on the peer.
  For the image this still requires the operator to push it to the registry.
* **Existing peers must run the install command once more** to get the
  update loop. An agent started by the old installer has no loop to restart
  it, so it simply exits on the first update. This is a one-time cost.
* Agents now restart briefly when an update lands. A restart takes a few
  seconds and keeps the node's identity; the φ-accrual detector may mark the
  node OFFLINE for those seconds, which is harmless while it holds no lease.
  The update check only runs while it holds none.
* **Verified so far:** a real agent installed at an old version exits with 75
  within one check interval, and stays up when the versions match. Both
  installers pass syntax checks, and their update loops are covered by tests
  on the served script text. **Not yet verified end to end:** an installer
  performing the download, reinstall and restart on a real peer. Running it on
  the development laptop would overwrite that machine's own agent identity.
