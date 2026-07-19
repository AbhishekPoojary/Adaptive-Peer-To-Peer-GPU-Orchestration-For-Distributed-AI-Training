"""Schemas for node enrollment, hardware inventory, and heartbeat telemetry.

Bounds mirror physical reality (no negative counts/bytes, utilisation in
[0, 100], temperature above absolute zero). Missing GPU telemetry is expressed
as ``null``, never as zeros — the ``gpu`` list and its optional per-GPU fields
default to / accept ``None`` rather than being invented.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, model_validator

_FORBID = ConfigDict(extra="forbid")


# --- Hardware inventory (reported at enrollment) -----------------------------


class GpuInfo(BaseModel):
    """One GPU's static inventory. Absent GPUs are represented by an empty list,
    never a fabricated entry."""

    model_config = _FORBID

    name: str = Field(min_length=1, max_length=256)
    vram_bytes: int = Field(ge=0)
    driver_version: str = Field(min_length=1, max_length=128)


class HardwareInventory(BaseModel):
    """Truthful static hardware inventory for a node."""

    model_config = _FORBID

    hostname: str = Field(min_length=1, max_length=256)
    os: str = Field(min_length=1, max_length=256)
    cpu_model: str = Field(min_length=1, max_length=256)
    cores: int = Field(ge=1)
    ram_bytes: int = Field(ge=0)
    # Empty list is valid and meaningful: a CPU-only node has no GPUs.
    gpus: list[GpuInfo] = Field(default_factory=list)


class NodeRegisterRequest(BaseModel):
    """Body of POST /nodes/register."""

    model_config = _FORBID

    enrollment_token: str = Field(min_length=1)
    # Agent-generated Ed25519 public key, PEM (SubjectPublicKeyInfo) encoded.
    public_key: str = Field(min_length=1)
    hardware: HardwareInventory
    agent_version: str = Field(min_length=1, max_length=64)


class NodeRegisterResponse(BaseModel):
    """Result of a successful enrollment: identity plus a first access token."""

    node_id: uuid.UUID
    name: str
    access_token: str
    token_type: str = "bearer"
    expires_in: int


# --- Heartbeat telemetry -----------------------------------------------------


class GpuTelemetry(BaseModel):
    """Per-GPU live telemetry. ``temperature_c``/``power_w`` are optional because
    a given driver may not expose them; when unavailable they are ``null``."""

    model_config = _FORBID

    util_percent: float = Field(ge=0, le=100)
    mem_used_bytes: int = Field(ge=0)
    mem_total_bytes: int = Field(ge=0)
    temperature_c: float | None = Field(default=None, ge=-273.15, le=200)
    power_w: float | None = Field(default=None, ge=0, le=10000)

    @model_validator(mode="after")
    def _mem_used_within_total(self) -> GpuTelemetry:
        if self.mem_used_bytes > self.mem_total_bytes:
            raise ValueError("mem_used_bytes cannot exceed mem_total_bytes")
        return self


class HeartbeatRequest(BaseModel):
    """Body of POST /nodes/{id}/heartbeat.

    ``gpu`` is ``null`` when the agent has no GPU telemetry (e.g. NVML absent);
    ``rtt_ms`` is ``null`` until the agent has measured a round trip. Neither is
    ever filled in server-side.
    """

    model_config = _FORBID

    cpu_percent: float = Field(ge=0, le=100)
    ram_used_bytes: int = Field(ge=0)
    ram_total_bytes: int = Field(ge=0)
    gpu: list[GpuTelemetry] | None = None
    rtt_ms: float | None = Field(default=None, ge=0)

    @model_validator(mode="after")
    def _ram_used_within_total(self) -> HeartbeatRequest:
        if self.ram_used_bytes > self.ram_total_bytes:
            raise ValueError("ram_used_bytes cannot exceed ram_total_bytes")
        return self


class HeartbeatResponse(BaseModel):
    """Heartbeat acknowledgement.

    ``server_time`` lets the agent measure the round trip client-side; the
    server never invents RTT. ``rtt_ewma_ms`` is the server's smoothed RTT and
    is ``null`` until at least one RTT has been measured.
    """

    server_time: datetime
    last_heartbeat_at: datetime
    status: str
    rtt_ewma_ms: float | None
