"""Application configuration, sourced entirely from environment variables.

No defaults are fabricated for anything security- or data-sensitive; every
value here maps 1:1 to a variable documented in deploy/.env.example.
"""

from __future__ import annotations

from functools import lru_cache

from pydantic import field_validator
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
    # Bucket holding uploaded training datasets (ADR-014). Separate from
    # checkpoints because the two have opposite access patterns and lifetimes: a
    # checkpoint is written by a trainer and read once on resume, a dataset is
    # written once by an operator and read by every peer that runs the job.
    s3_bucket_datasets: str = "datasets"

    # --- Custom datasets (ADR-014) ---
    # Hard ceiling on an uploaded archive, in bytes. Enforced while streaming to
    # disk, so an oversized upload is cut off rather than buffered first.
    # 2 GiB default: large enough for a real image set, small enough that one
    # upload cannot fill the orchestrator's disk.
    dataset_max_upload_bytes: int = 2 * 1024 * 1024 * 1024
    # Ceiling on the *decompressed* size. A zip bomb is small on the wire and
    # enormous on disk, so the compressed cap above cannot catch it; this is
    # checked against the archive's declared sizes before extracting anything.
    dataset_max_uncompressed_bytes: int = 8 * 1024 * 1024 * 1024
    # Ceiling on how many files an archive may contain. Bounds the validation
    # walk itself, which would otherwise be a cheap way to occupy the server.
    dataset_max_files: int = 200_000
    # Bounds on how many classes a dataset may declare. One class cannot be
    # classified; the upper bound stops a mislaid directory tree from being read
    # as tens of thousands of labels.
    dataset_min_classes: int = 2
    dataset_max_classes: int = 1000
    # Lifetime of the presigned URL a peer is handed when it claims a job. Long
    # enough to download a large archive on a slow home connection, short enough
    # that a peer which has finished cannot keep reading the data indefinitely.
    # It is minted per claim, so a retry on another node gets a fresh one rather
    # than reusing this window.
    dataset_url_ttl_seconds: int = 3600

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

    # --- Google sign-in (ADR-012 addendum) ---
    # OAuth *client ID* for the Google Identity Services button. Public by
    # design — it ships in the dashboard bundle and is served from
    # GET /auth/providers, which is why there is no client *secret* anywhere in
    # this file: the ID-token flow never uses one (ADR-012 addendum §2).
    #
    # None disables Google sign-in entirely: POST /auth/google returns 503 and
    # the dashboard renders only the password form. That default is what keeps
    # an offline deployment working, so it is not merely a convenience.
    google_oauth_client_id: str | None = None
    # Reject a Google ID token issued longer ago than this. Google mints them
    # with roughly an hour of validity; a stolen one is a bearer credential for
    # its whole lifetime, and nothing in the token makes it single-use. Demanding
    # freshness shrinks the replay window from ~1 h to minutes at zero cost,
    # because a real sign-in posts the token within seconds of receiving it.
    google_id_token_max_age_seconds: int = 300
    # How long Google's signing keys (JWKS) are cached. Google rotates them
    # slowly and publishes the rotation ahead of use; an hour keeps a normal
    # sign-in off the network path without pinning a retired key for long. A
    # cache miss on an unknown `kid` forces a refetch regardless of this.
    google_jwks_cache_seconds: int = 3600
    # Timeout for fetching Google's JWKS. Short and explicit: a hung fetch would
    # otherwise stall the login endpoint for a client that cannot be helped.
    google_jwks_timeout_seconds: float = 5.0

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
    # How many times a job may be retried after a *reported* trainer failure
    # before it fails terminally (ADR-005 addendum 2). ADR-005 originally made
    # such failures terminal so a broken job spec could not walk the whole
    # fleet; that is still the concern, and a small bound answers it while
    # giving a node-specific failure — an OOM kill on a 4 GB laptop GPU being
    # the likeliest real one here — a real second chance on different hardware.
    # 0 restores the strict pre-M11 fail-fast behaviour exactly.
    max_job_failure_retries: int = 2
    # How long the node whose trainer just failed is skipped, so the retry
    # prefers different hardware. Longer than the unclaimed backoff: a node that
    # killed a trainer is more likely to kill the next one than a node that was
    # merely slow to poll.
    failed_attempt_backoff_seconds: float = 45.0

    # Trainer image every peer launches. Served into the installers so a peer
    # never has to be told it separately and the fleet cannot drift onto mixed
    # images. The default is the locally-built name, which only exists on a
    # machine that built it — set this to a published reference
    # (e.g. docker.io/<user>/gpu-orchestrator-trainer:latest) and peers with
    # Docker will simply pull it.
    trainer_image: str = "gpu-orchestrator-trainer:latest"

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

    @field_validator("google_oauth_client_id", mode="after")
    @classmethod
    def _blank_client_id_is_absent(cls, value: str | None) -> str | None:
        """Treat an empty or whitespace-only client ID as unset.

        Not defensive padding — it is the difference between Google sign-in being
        off and being advertised as on while broken. ``deploy/compose.yaml`` sets
        this with ``${GOOGLE_OAUTH_CLIENT_ID:-}``, and Compose's ``:-`` default
        makes the variable *present and empty* rather than absent. Without this,
        an unconfigured deployment would report ``enabled: true`` from
        ``GET /auth/providers``, the dashboard would draw a Google button, and it
        would fail inside Google's script with nothing pointing back at the cause.
        """
        if value is None:
            return None
        stripped = value.strip()
        return stripped or None


@lru_cache
def get_settings() -> Settings:
    """Return the process-wide cached Settings instance."""
    return Settings()
