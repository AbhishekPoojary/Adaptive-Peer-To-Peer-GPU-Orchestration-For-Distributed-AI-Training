"""Agent entrypoint (skeleton).

M0 scope: package structure only. The real agent will enroll with a
one-time token (ADR-008), dial out to the orchestrator over HTTPS/WebSocket
(ADR-002), and report real telemetry (never fabricated) on a fixed cadence.
"""

from __future__ import annotations


def main() -> None:
    raise NotImplementedError(
        "agent runtime is implemented in a later milestone (M0 ships the "
        "package skeleton only)"
    )


if __name__ == "__main__":
    main()
