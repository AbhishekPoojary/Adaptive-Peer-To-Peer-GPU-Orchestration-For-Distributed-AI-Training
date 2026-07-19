# ADR-008: Identity

## Status
Accepted

## Context
Peer nodes need to authenticate to the orchestrator without a manual PKI
setup per node, and the orchestrator needs to authenticate agent requests
on an ongoing basis without repeatedly transmitting a long-lived shared
secret.

## Decision
Three-stage identity:
1. **One-time enrollment token**: operator generates a short-lived,
   single-use token out of band and gives it to the new node.
2. **Per-node keypair**: on first run, the agent generates its own
   keypair locally and uses the enrollment token exactly once to register
   the public key with the orchestrator. The private key never leaves the
   node.
3. **Short-lived JWT**: subsequent authentication uses JWTs issued by the
   orchestrator (signed with `JWT_SIGNING_KEY`) after the agent proves
   possession of its registered keypair, with a short TTL requiring
   periodic reissue.

An optional mTLS profile is available for deployments that want
transport-level mutual auth in addition to the JWT layer.

## Consequences
- The enrollment token is single-use and expiring, so leaking one old
  token doesn't grant standing access — it only works for the enrollment
  window.
- Compromising a node's private key compromises that node only, not the
  fleet (no shared secret across nodes).
- Short-lived JWTs mean revocation is naturally bounded by TTL even
  without an explicit revocation list, at the cost of more frequent
  reissue traffic.
- mTLS is optional, not default, because it adds certificate lifecycle
  management that isn't justified for the single-laptop dev topology in
  ADR-010 but may be required for some multi-host deployments.
