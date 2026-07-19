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
    # Admin bootstrap key for POST /auth/enrollment-tokens. No default: absent
    # means the admin surface is disabled, and startup is refused outside dev
    # (see orchestrator.main.lifespan). Never bake a real key into an image.
    admin_api_key: str | None = None
    # Challenge-response nonce lifetime for POST /auth/token/refresh.
    auth_nonce_ttl_seconds: int = 120

    # --- Telemetry / RTT (ADR-004) ---
    # Smoothing factor for the round-trip-time EWMA the heartbeat handler
    # maintains from agent-measured RTT. Never used to invent an RTT — only to
    # smooth measured values. 0 < alpha <= 1; higher weights recent samples.
    rtt_ewma_alpha: float = 0.3

    # --- Reliability prior (ADR-009) ---
    # Declared Beta(alpha, beta) prior for a freshly enrolled node before any
    # lease history exists. Beta(1, 1) is the uniform prior: no reliability is
    # assumed, it is derived from recorded lease outcomes as they accrue.
    reliability_prior_alpha: float = 1.0
    reliability_prior_beta: float = 1.0

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

    # --- Read API (M1) ---
    # A node is reported `heartbeat_stale: true` when now - last_heartbeat_at
    # exceeds this window. Computed on read only — it never mutates node
    # status; that is the failure detector's job (ADR-004, M6).
    heartbeat_stale_seconds: float = 15.0
    # Default / max number of telemetry samples GET /nodes/{id} returns.
    node_detail_default_samples: int = 50
    node_detail_max_samples: int = 500


@lru_cache
def get_settings() -> Settings:
    """Return the process-wide cached Settings instance."""
    return Settings()
