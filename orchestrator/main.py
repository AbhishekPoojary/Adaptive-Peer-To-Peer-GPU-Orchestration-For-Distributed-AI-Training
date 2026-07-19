"""FastAPI application entrypoint."""

from __future__ import annotations

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from orchestrator.api.health import router as health_router
from orchestrator.core.config import get_settings
from orchestrator.core.db import dispose_engine, get_engine
from orchestrator.core.logging import configure_logging


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    settings = get_settings()
    configure_logging(settings.log_level)
    get_engine(settings)
    yield
    await dispose_engine()


def create_app() -> FastAPI:
    """Application factory."""
    app = FastAPI(title="GPU Orchestrator", version="0.1.0", lifespan=lifespan)
    app.include_router(health_router)
    return app


app = create_app()
