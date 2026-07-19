"""Async SQLAlchemy engine/session management."""

from __future__ import annotations

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

from sqlalchemy import text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase

from orchestrator.core.config import Settings, get_settings


class Base(DeclarativeBase):
    """Declarative base for all ORM models."""


_engine: AsyncEngine | None = None
_sessionmaker: async_sessionmaker[AsyncSession] | None = None


def get_engine(settings: Settings | None = None) -> AsyncEngine:
    """Return (creating if needed) the process-wide async engine."""
    global _engine
    if _engine is None:
        settings = settings or get_settings()
        _engine = create_async_engine(
            settings.database_url,
            pool_size=settings.db_pool_size,
            max_overflow=settings.db_pool_max_overflow,
            pool_pre_ping=True,
        )
    return _engine


def get_sessionmaker(settings: Settings | None = None) -> async_sessionmaker[AsyncSession]:
    """Return (creating if needed) the process-wide session factory."""
    global _sessionmaker
    if _sessionmaker is None:
        _sessionmaker = async_sessionmaker(
            bind=get_engine(settings), expire_on_commit=False
        )
    return _sessionmaker


@asynccontextmanager
async def session_scope() -> AsyncGenerator[AsyncSession, None]:
    """Provide a transactional session as an async context manager."""
    maker = get_sessionmaker()
    async with maker() as session:
        yield session


async def get_session() -> AsyncGenerator[AsyncSession, None]:
    """FastAPI dependency yielding a request-scoped session."""
    maker = get_sessionmaker()
    async with maker() as session:
        yield session


async def check_database_connection(settings: Settings | None = None) -> bool:
    """Run a real `SELECT 1` against Postgres. Returns True only on genuine success.

    Never fabricates a healthy result: any exception (network, auth, DNS,
    timeout) is treated as db-down.
    """
    try:
        engine = get_engine(settings)
        async with engine.connect() as conn:
            result = await conn.execute(text("SELECT 1"))
            return bool(result.scalar_one() == 1)
    except Exception:
        return False


async def dispose_engine() -> None:
    """Dispose of the process-wide engine (used on shutdown and in tests)."""
    global _engine, _sessionmaker
    if _engine is not None:
        await _engine.dispose()
    _engine = None
    _sessionmaker = None
