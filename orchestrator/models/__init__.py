"""SQLAlchemy ORM models (M1: nodes, telemetry, enrollment, auth challenges).

Importing this package registers every model on ``Base.metadata`` so Alembic's
``env.py`` and any ``create_all`` see the full schema. Add new model modules to
the imports below when they land.
"""

from __future__ import annotations

from orchestrator.models.enrollment import EnrollmentToken
from orchestrator.models.node import Node, NodeStatus, NodeTelemetrySample
from orchestrator.models.nonce import AuthChallenge

__all__ = [
    "AuthChallenge",
    "EnrollmentToken",
    "Node",
    "NodeStatus",
    "NodeTelemetrySample",
]
