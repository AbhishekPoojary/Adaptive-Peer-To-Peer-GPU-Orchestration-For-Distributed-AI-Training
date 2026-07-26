"""`GET /metrics`: Prometheus text-format exposition (M7).

Unauthenticated, like the rest of the dev-mode read surface (see the M8 TODOs
on ``nodes``/``jobs``) — a metrics scrape endpoint is conventionally
unauthenticated on a private network and gated at the network/reverse-proxy
layer in production, not by an application-level key.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Response
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
from sqlalchemy.ext.asyncio import AsyncSession

from orchestrator.core.db import get_session
from orchestrator.core.metrics import REGISTRY, update_db_gauges

router = APIRouter()


@router.get("/metrics")
async def get_metrics(session: AsyncSession = Depends(get_session)) -> Response:
    """Real counters (incremented at their event's call site) plus gauges
    freshly computed from the live database — never a cached or invented
    value."""
    await update_db_gauges(session)
    return Response(content=generate_latest(REGISTRY), media_type=CONTENT_TYPE_LATEST)
