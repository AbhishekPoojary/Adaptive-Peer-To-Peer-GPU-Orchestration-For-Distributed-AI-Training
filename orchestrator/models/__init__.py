"""SQLAlchemy ORM models.

Importing this package registers every model on ``Base.metadata`` so Alembic's
``env.py`` and any ``create_all`` see the full schema, and configures the
cross-model relationships (jobs ↔ leases resolve their string-form references
here). Add new model modules to the imports below when they land.
"""

from __future__ import annotations

from orchestrator.models.enrollment import EnrollmentToken
from orchestrator.models.job import (
    Job,
    JobEvent,
    JobState,
    can_transition,
    is_terminal,
)
from orchestrator.models.lease import Lease, LeaseState
from orchestrator.models.node import Node, NodeStatus, NodeTelemetrySample
from orchestrator.models.nonce import AuthChallenge
from orchestrator.models.scheduling import (
    SchedulingDecision,
    SchedulingDecisionCandidate,
)
from orchestrator.models.training import TrainingLogLine, TrainingMetric

__all__ = [
    "AuthChallenge",
    "EnrollmentToken",
    "Job",
    "JobEvent",
    "JobState",
    "Lease",
    "LeaseState",
    "Node",
    "NodeStatus",
    "NodeTelemetrySample",
    "SchedulingDecision",
    "SchedulingDecisionCandidate",
    "TrainingLogLine",
    "TrainingMetric",
    "can_transition",
    "is_terminal",
]
