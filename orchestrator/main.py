"""FastAPI application entrypoint."""

from __future__ import annotations

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from orchestrator.api.auth import router as auth_router
from orchestrator.api.health import router as health_router
from orchestrator.api.jobs import router as jobs_router
from orchestrator.api.leases import router as leases_router
from orchestrator.api.nodes import router as nodes_router
from orchestrator.core.config import Settings, get_settings
from orchestrator.core.db import dispose_engine, get_engine
from orchestrator.core.logging import configure_logging
from orchestrator.services.loops import start_background_loops, stop_background_loops

# APP_ENV values treated as development/test, where dev-only defaults are
# tolerated. Anything else is a real deployment and must supply real secrets.
_DEV_ENVS = frozenset({"dev", "test", "local"})
_JWT_DEV_DEFAULT = "dev-only-change-me"


def _enforce_production_secrets(settings: Settings) -> None:
    """Refuse startup outside dev if security-critical secrets are unset.

    ADR-008: there is no default admin key or JWT signing key in production
    paths. In a non-dev APP_ENV, a missing ADMIN_API_KEY or the dev-default
    JWT_SIGNING_KEY is a hard startup failure, not a silent insecure default.
    """
    if settings.app_env.lower() in _DEV_ENVS:
        return
    missing: list[str] = []
    if not settings.admin_api_key:
        missing.append("ADMIN_API_KEY")
    if settings.jwt_signing_key == _JWT_DEV_DEFAULT:
        missing.append("JWT_SIGNING_KEY")
    if missing:
        raise RuntimeError(
            f"Refusing to start in APP_ENV={settings.app_env!r}: "
            f"{', '.join(missing)} must be set to a real secret."
        )


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    settings = get_settings()
    configure_logging(settings.log_level)
    _enforce_production_secrets(settings)
    get_engine(settings)
    loop_tasks = (
        start_background_loops(settings) if settings.enable_background_loops else []
    )
    try:
        yield
    finally:
        await stop_background_loops(loop_tasks)
        await dispose_engine()


def create_app() -> FastAPI:
    """Application factory."""
    app = FastAPI(title="GPU Orchestrator", version="0.1.0", lifespan=lifespan)
    app.include_router(health_router)
    app.include_router(auth_router)
    app.include_router(nodes_router)
    app.include_router(jobs_router)
    app.include_router(leases_router)
    return app


app = create_app()
