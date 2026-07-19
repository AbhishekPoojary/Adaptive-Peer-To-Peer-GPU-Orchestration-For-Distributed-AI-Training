"""Health check endpoint.

`/health` only ever reports what it actually measured. The db field is
"ok" solely after a real SELECT 1 succeeds, and "down" otherwise — the
overall status is "ok" only when every checked dependency is healthy.
There is no fabricated success path.
"""

from __future__ import annotations

from fastapi import APIRouter, Response, status

from orchestrator.core.db import check_database_connection

router = APIRouter()


@router.get("/health")
async def health(response: Response) -> dict[str, str]:
    """Report liveness plus a real database connectivity probe."""
    db_ok = await check_database_connection()
    if not db_ok:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    return {
        "status": "ok" if db_ok else "degraded",
        "db": "ok" if db_ok else "down",
    }
