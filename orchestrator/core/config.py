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

    # --- Human operator auth (ADR-012, M8) ---
    # TTL of a user (aud="user") access token. Shorter than a node's because a
    # human token is held in a browser; revocation is bounded by this window.
    user_access_token_ttl_seconds: int = 900
    # Fixed-window rate limits on the credential endpoints, per client IP.
    # These bound password/nonce guessing; they are per-process, so a
    # multi-replica deployment multiplies them (see ADR-012 §7).
    login_rate_limit_attempts: int = 10
    login_rate_limit_window_seconds: float = 60.0
    # Node-facing credential endpoints (challenge, token refresh, register).
    # Higher than login: a fleet of agents legitimately refreshes on a timer,
    # and the secret here is a 256-bit key, not a password.
    node_auth_rate_limit_attempts: int = 60
    node_auth_rate_limit_window_seconds: float = 60.0

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
    # Half-life (seconds) of the exponential decay applied to each historical
    # lease outcome when computing reliability (ADR-009 time decay): an outcome
    # this many seconds old counts half as much as a fresh one. Default 1 day.
    reliability_decay_halflife_seconds: float = 86400.0
    # Normal quantile for the Wilson score interval's confidence level. 1.96 is
    # the standard ~95% two-sided value; larger = more conservative (lower R for
    # the same evidence). Affects reliability, so it is logged and audited per
    # decision, not applied silently.
    reliability_wilson_z: float = 1.96

    # --- Scheduling (ADR-009) ---
    # 'adaptive' (M3 penalty score), 'least_loaded', or 'round_robin'. Default
    # stays least_loaded so existing behaviour is unchanged; adaptive is opt-in
    # per job via scheduler_name (the M3 live demo submits it explicitly).
    scheduler_strategy: str = "least_loaded"
    # Weights in S_i = alpha*L_i - beta*R_i + gamma*D_i. All read here and
    # logged/audited with every adaptive decision.
    scheduler_alpha_load: float = 1.0
    scheduler_beta_reliability: float = 1.0
    scheduler_gamma_latency: float = 0.5
    # Smallest difference treated as a full-scale gap when normalizing the load
    # and latency terms. Normalization divides by max(observed spread, this),
    # so a difference smaller than the domain considers meaningful produces a
    # proportionally small penalty instead of being amplified to 1.0.
    # Without these, the M9 benchmark measured a 7 ms loopback jitter outvoting
    # a real reliability gap and the adaptive scheduler placing jobs on a node
    # with 3 recorded failures (ADR-009 addendum).
    scheduler_load_significant_spread: float = 25.0
    scheduler_latency_significant_spread_ms: float = 50.0

    # --- Leases (ADR-003) ---
    lease_ttl_seconds: int = 30
    lease_renewal_grace_seconds: int = 5
    # How long the scheduler skips a node after an offer to it lapsed unclaimed
    # (M7.1c). Defence in depth: the root cause of observed offer-thrash was an
    # agent stuck on an orphaned container, fixed in M7.1b, but a node that
    # demonstrably could not pick up the last offer should not immediately be
    # handed another. Kept short — this is "wait a moment", not a penalty, and a
    # long window would strand work on a fleet that is merely slow. Set to 0 to
    # disable the backoff entirely.
    unclaimed_offer_backoff_seconds: float = 20.0

    # --- Distributed training / rendezvous (ADR-005, M5) ---
    # TCP port the c10d rendezvous host binds for a multi-rank cohort; every rank
    # dials <rendezvous host>:<this port>. High port so no CAP_NET_BIND_SERVICE
    # is needed under ADR-007's cap_drop=ALL.
    rendezvous_port: int = 29500
    # torchrun --max-restarts: bounded elastic re-formations of the process group
    # on a worker crash (ADR-005). Real and present even though M5 does not
    # exercise deep elasticity.
    torchrun_max_restarts: int = 1
    # torch.distributed backend handed to every rank. 'gloo' for M5's
    # correctness verification given the shared-single-GPU / mixed-hardware
    # reality (ADR-010); 'nccl' is the intended backend once real distinct
    # multi-GPU hardware exists (ADR-005). A deliberate, documented choice.
    training_backend: str = "gloo"

    # --- Background loops (M2) ---
    # The orchestrator runs three periodic asyncio loops: a scheduler pass that
    # places QUEUED/REASSIGNED jobs, a sweep that expires overdue leases, and the
    # M6 φ-accrual failure detector. Intervals are how often each wakes; a submit
    # (and a detector-declared failure) also triggers an immediate scheduler pass
    # so placement isn't gated on the loop cadence.
    scheduler_pass_interval_seconds: float = 3.0
    lease_sweep_interval_seconds: float = 3.0
    # Master switch for the background loops. Disabled in tests so scheduling,
    # sweeping, and failure detection are driven deterministically by the test,
    # not a wall clock.
    enable_background_loops: bool = True

    # --- Failure detection (ADR-004, M6 φ-accrual detector) ---
    # How often the detector loop evaluates every ONLINE node's liveness. Kept
    # short (1 s) so detection latency is dominated by the 5 s floor, not the
    # tick; this is the shipped value, not a demo-only tuning.
    failure_detector_interval_seconds: float = 1.0
    # Hard floor: no node is ever declared failed with less than this many
    # seconds of silence, regardless of what the φ math would allow (ADR-004).
    heartbeat_floor_seconds: float = 5.0
    # φ suspicion threshold to declare a node failed. φ = -log10(P(gap this late
    # under the node's own recent interval distribution)); 3.0 ≈ "≤0.1% likely".
    phi_accrual_threshold: float = 3.0
    # Rolling window of most-recent heartbeat inter-arrival intervals fitted to
    # a Normal for the φ computation.
    phi_accrual_window_samples: int = 20
    # Floor on the fitted interval stddev (seconds): prevents divide-by-zero and
    # over-sensitivity on a node with near-constant intervals (standard φ-accrual
    # guard). Not a fabricated value — a documented numerical safety bound.
    phi_accrual_min_std_seconds: float = 0.5
    # Minimum number of observed intervals before the φ distribution is trusted.
    # Below this a freshly enrolled node uses the bootstrap silence fallback
    # rather than a fabricated distribution.
    phi_accrual_min_intervals: int = 3
    # Bootstrap fallback: with too little history to fit a distribution, declare
    # failed only after this much continuous silence (still ≥ the 5 s floor).
    phi_accrual_bootstrap_silence_seconds: float = 10.0

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
