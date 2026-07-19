# ADR-002: Agent transport

## Status
Accepted

## Context
Agents run on peer machines (home laptops, NATed networks, no public IP,
no port-forwarding assumed). The orchestrator must be reachable by agents
without agents being reachable by the orchestrator, and must support both
occasional lifecycle calls (enroll, claim job, report result) and
continuous streams (heartbeat, logs, metrics).

## Decision
Two transports, both agent-initiated:
- **HTTPS REST** for lifecycle operations: enrollment, lease claim/renew,
  job status transitions, checkpoint manifest registration.
- **WebSocket** for continuous streams: heartbeat, log tail, live metric
  push. The agent dials out and holds the connection open; the
  orchestrator never initiates a connection to an agent.

## Consequences
- NAT-friendly by construction: no inbound firewall rules or port
  forwarding required on peer machines, which is required for the
  dev/multi-host topology in ADR-010.
- The orchestrator must handle reconnect/resume semantics on the
  WebSocket side (agents will drop and redial); heartbeat gaps during a
  reconnect feed the failure detector in ADR-004 rather than being
  masked.
- REST calls are naturally retryable/idempotent-by-design (lease claim
  uses epochs, ADR-003); WebSocket messages are not assumed durable, so
  anything that must survive a disconnect goes through REST instead.
