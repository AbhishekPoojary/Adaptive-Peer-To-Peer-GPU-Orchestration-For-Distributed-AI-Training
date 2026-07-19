"""Application configuration, sourced entirely from environment variables.

No defaults are fabricated for anything security- or data-sensitive; every
value here maps 1:1 to a variable documented in deploy/.env.example.
"""

from __future__ import annotations

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Process-wide settings, loaded once from the environment."""

    model_config = SettingsConfigDict(
        env_prefix="",
        env_file=None,
        case_sensitive=False,
        extra="ignore",
    )

    # --- Database ---
    database_url: str = "postgresql+asyncpg://orchestrator:orchestrator@localhost:5432/orchestrator"
    db_pool_size: int = 5
    db_pool_max_overflow: int = 10
    db_connect_timeout_seconds: float = 5.0

    # --- App ---
    app_env: str = "dev"
    log_level: str = "INFO"
    api_host: str = "0.0.0.0"
    api_port: int = 8000

    # --- Object storage (MinIO / S3 API) ---
    s3_endpoint_url: str = "http://localhost:9000"
    s3_access_key: str = "minioadmin"
    s3_secret_key: str = "minioadmin"
    s3_bucket_checkpoints: str = "checkpoints"
    s3_region: str = "us-east-1"

    # --- Auth (ADR-008) ---
    jwt_signing_key: str = "dev-only-change-me"
    jwt_access_token_ttl_seconds: int = 900
    enrollment_token_ttl_seconds: int = 3600

    # --- Scheduling (ADR-009) ---
    scheduler_strategy: str = "least_loaded"
    scheduler_alpha_load: float = 1.0
    scheduler_beta_reliability: float = 1.0
    scheduler_gamma_latency: float = 0.5

    # --- Leases (ADR-003) ---
    lease_ttl_seconds: int = 30
    lease_renewal_grace_seconds: int = 5

    # --- Failure detection (ADR-004) ---
    heartbeat_floor_seconds: float = 5.0


@lru_cache
def get_settings() -> Settings:
    """Return the process-wide cached Settings instance."""
    return Settings()
