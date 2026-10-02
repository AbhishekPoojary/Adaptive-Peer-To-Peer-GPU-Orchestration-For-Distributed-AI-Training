"""Docker launch flag construction (ADR-007, M4).

``build_run_kwargs`` is pure and side-effect free specifically so the exact
isolation profile can be asserted without a real Docker daemon. ``FakeDockerClient``
below is a lightweight test double (CONTRIBUTING.md #5: fakes live only in
tests/, named ``Fake*``) standing in for the real ``docker.DockerClient`` to
exercise ``ensure_dataset_cache_volume``/``launch_trainer_container`` too.
"""

from __future__ import annotations

from typing import Any

import docker
import docker.errors
import pytest

from agent.runtime.docker_launcher import (
    RendezvousSpec,
    TrainerLaunchConfig,
    build_run_kwargs,
    ensure_dataset_cache_volume,
    ensure_rendezvous_network,
    launch_trainer_container,
)

_JOB_SPEC = {
    "dataset": "cifar10",
    "model": "small_cnn",
    "epochs": 5,
    "batch_size": 128,
    "learning_rate": 0.001,
    "world_size": 1,
    "min_gpu_mem_bytes": None,
}


class FakeVolume:
    def __init__(self, name: str) -> None:
        self.name = name


class FakeVolumesApi:
    """Stands in for ``docker.DockerClient.volumes``."""

    def __init__(self, *, existing: list[str] | None = None) -> None:
        self._existing = set(existing or [])
        self.created: list[str] = []

    def get(self, name: str) -> FakeVolume:
        if name not in self._existing:
            raise docker.errors.NotFound(f"no such volume: {name}")
        return FakeVolume(name)

    def create(self, name: str) -> FakeVolume:
        self.created.append(name)
        self._existing.add(name)
        return FakeVolume(name)


class FakeContainer:
    def __init__(self, run_kwargs: dict[str, Any]) -> None:
        self.run_kwargs = run_kwargs
        self.id = "fake-container-id"


class FakeContainersApi:
    """Stands in for ``docker.DockerClient.containers``."""

    def __init__(self) -> None:
        self.run_calls: list[dict[str, Any]] = []

    def run(self, **kwargs: Any) -> FakeContainer:
        self.run_calls.append(kwargs)
        return FakeContainer(kwargs)


class FakeNetwork:
    def __init__(self, name: str) -> None:
        self.name = name


class FakeNetworksApi:
    """Stands in for ``docker.DockerClient.networks``."""

    def __init__(self, *, existing: list[str] | None = None) -> None:
        self._existing = set(existing or [])
        self.created: list[str] = []

    def get(self, name: str) -> FakeNetwork:
        if name not in self._existing:
            raise docker.errors.NotFound(f"no such network: {name}")
        return FakeNetwork(name)

    def create(self, name: str, **kwargs: Any) -> FakeNetwork:
        self.created.append(name)
        self._existing.add(name)
        return FakeNetwork(name)


class FakeDockerClient:
    """A minimal stand-in for ``docker.DockerClient`` (tests/ test double)."""

    def __init__(
        self,
        *,
        existing_volumes: list[str] | None = None,
        existing_networks: list[str] | None = None,
    ) -> None:
        self.volumes = FakeVolumesApi(existing=existing_volumes)
        self.containers = FakeContainersApi()
        self.networks = FakeNetworksApi(existing=existing_networks)


# --- build_run_kwargs: the exact ADR-007 isolation profile -------------------


def _config(**overrides: Any) -> TrainerLaunchConfig:
    defaults: dict[str, Any] = {
        "image": "gpu-orchestrator-trainer:latest",
        "memory_limit": "6g",
        "pids_limit": 256,
        "dataset_cache_volume": "gpu-orchestrator-dataset-cache",
    }
    defaults.update(overrides)
    return TrainerLaunchConfig(**defaults)


def test_environment_carries_the_job_spec_and_lease_identity() -> None:
    kwargs = build_run_kwargs(
        config=_config(),
        job_spec=_JOB_SPEC,
        job_id="job-1",
        lease_id="lease-1",
        lease_epoch=3,
        has_gpu=False,
    )
    assert kwargs["environment"] == {
        "DATASET": "cifar10",
        "MODEL": "small_cnn",
        "EPOCHS": "5",
        "BATCH_SIZE": "128",
        "LEARNING_RATE": "0.001",
        "JOB_ID": "job-1",
        "LEASE_ID": "lease-1",
        "LEASE_EPOCH": "3",
    }


def test_isolation_flags_match_adr_007_exactly() -> None:
    kwargs = build_run_kwargs(
        config=_config(),
        job_spec=_JOB_SPEC,
        job_id="job-1",
        lease_id="lease-1",
        lease_epoch=1,
        has_gpu=False,
    )
    assert kwargs["remove"] is True  # --rm
    assert kwargs["cap_drop"] == ["ALL"]
    assert kwargs["security_opt"] == ["no-new-privileges"]
    assert kwargs["read_only"] is True
    assert kwargs["tmpfs"] == {"/tmp": "rw,noexec,nosuid,size=2g"}
    assert kwargs["network_mode"] == "bridge"
    assert kwargs["privileged"] is False
    assert "cap_add" not in kwargs


def test_dataset_cache_volume_mounted_at_data_cache() -> None:
    kwargs = build_run_kwargs(
        config=_config(dataset_cache_volume="my-node-cache"),
        job_spec=_JOB_SPEC,
        job_id="job-1",
        lease_id="lease-1",
        lease_epoch=1,
        has_gpu=False,
    )
    assert kwargs["volumes"] == {"my-node-cache": {"bind": "/data-cache", "mode": "rw"}}


def test_memory_and_pids_limit_come_from_config() -> None:
    kwargs = build_run_kwargs(
        config=_config(memory_limit="3g", pids_limit=128),
        job_spec=_JOB_SPEC,
        job_id="job-1",
        lease_id="lease-1",
        lease_epoch=1,
        has_gpu=False,
    )
    assert kwargs["mem_limit"] == "3g"
    assert kwargs["pids_limit"] == 128


def test_no_gpu_device_request_when_node_has_no_gpu() -> None:
    kwargs = build_run_kwargs(
        config=_config(),
        job_spec=_JOB_SPEC,
        job_id="job-1",
        lease_id="lease-1",
        lease_epoch=1,
        has_gpu=False,
    )
    assert "device_requests" not in kwargs


def test_gpu_device_request_attached_only_when_node_has_a_gpu() -> None:
    kwargs = build_run_kwargs(
        config=_config(),
        job_spec=_JOB_SPEC,
        job_id="job-1",
        lease_id="lease-1",
        lease_epoch=1,
        has_gpu=True,
    )
    assert "device_requests" in kwargs
    requests = kwargs["device_requests"]
    assert len(requests) == 1
    device_request = requests[0]
    # docker.types.DeviceRequest(count=-1, capabilities=[["gpu"]]) == "--gpus all"
    assert device_request["Count"] == -1
    assert device_request["Capabilities"] == [["gpu"]]


def test_never_uses_host_networking() -> None:
    kwargs = build_run_kwargs(
        config=_config(),
        job_spec=_JOB_SPEC,
        job_id="job-1",
        lease_id="lease-1",
        lease_epoch=1,
        has_gpu=True,
    )
    assert kwargs["network_mode"] != "host"


def test_image_comes_from_config() -> None:
    kwargs = build_run_kwargs(
        config=_config(image="custom-trainer:v2"),
        job_spec=_JOB_SPEC,
        job_id="job-1",
        lease_id="lease-1",
        lease_epoch=1,
        has_gpu=False,
    )
    assert kwargs["image"] == "custom-trainer:v2"


# --- ensure_dataset_cache_volume / launch_trainer_container ------------------


def test_ensure_dataset_cache_volume_creates_when_absent() -> None:
    client = FakeDockerClient(existing_volumes=[])
    ensure_dataset_cache_volume(client, name="gpu-orchestrator-dataset-cache")  # type: ignore[arg-type]
    assert client.volumes.created == ["gpu-orchestrator-dataset-cache"]


def test_ensure_dataset_cache_volume_is_a_noop_when_present() -> None:
    client = FakeDockerClient(existing_volumes=["gpu-orchestrator-dataset-cache"])
    ensure_dataset_cache_volume(client, name="gpu-orchestrator-dataset-cache")  # type: ignore[arg-type]
    assert client.volumes.created == []


def test_launch_trainer_container_passes_kwargs_through() -> None:
    client = FakeDockerClient()
    kwargs = build_run_kwargs(
        config=_config(),
        job_spec=_JOB_SPEC,
        job_id="job-1",
        lease_id="lease-1",
        lease_epoch=1,
        has_gpu=False,
    )
    container = launch_trainer_container(client, run_kwargs=kwargs)  # type: ignore[arg-type]
    assert len(client.containers.run_calls) == 1
    assert client.containers.run_calls[0] == kwargs
    assert container.run_kwargs == kwargs


@pytest.mark.parametrize("has_gpu", [True, False])
def test_no_privileged_and_no_extra_cap_add_regardless_of_gpu(has_gpu: bool) -> None:
    kwargs = build_run_kwargs(
        config=_config(),
        job_spec=_JOB_SPEC,
        job_id="job-1",
        lease_id="lease-1",
        lease_epoch=1,
        has_gpu=has_gpu,
    )
    assert kwargs["privileged"] is False
    assert "cap_add" not in kwargs


# --- Multi-rank torchrun launch (M5, ADR-005) --------------------------------


def _rdzv(**overrides: Any) -> RendezvousSpec:
    defaults: dict[str, Any] = {
        "world_size": 2,
        "rank": 0,
        "is_rendezvous_host": True,
        "backend": "gloo",
        "endpoint": "gpuorch-rdzv-abc123-r0:29500",
        "rdzv_id": "abc123-1",
        "network": "gpuorch-rdzv-abc123",
        "host_alias": "gpuorch-rdzv-abc123-r0",
        "max_restarts": 1,
    }
    defaults.update(overrides)
    return RendezvousSpec(**defaults)


def test_ddp_launches_under_torchrun_with_real_c10d_flags() -> None:
    kwargs = build_run_kwargs(
        config=_config(),
        job_spec={**_JOB_SPEC, "world_size": 2},
        job_id="job-1",
        lease_id="lease-1",
        lease_epoch=1,
        has_gpu=False,
        rendezvous=_rdzv(world_size=2, max_restarts=3),
    )
    assert kwargs["command"] == [
        "torchrun",
        "--nnodes=2",
        "--nproc-per-node=1",
        "--rdzv-backend=c10d",
        "--rdzv-id=abc123-1",
        "--rdzv-endpoint=gpuorch-rdzv-abc123-r0:29500",
        "--max-restarts=3",
        "train.py",
    ]
    # The backend is passed to the trainer; DDP shares the cohort network.
    assert kwargs["environment"]["DIST_BACKEND"] == "gloo"
    assert kwargs["network"] == "gpuorch-rdzv-abc123"
    assert "network_mode" not in kwargs  # not the default-bridge single path


def test_ddp_rendezvous_host_takes_the_agreed_container_name() -> None:
    host = build_run_kwargs(
        config=_config(),
        job_spec={**_JOB_SPEC, "world_size": 2},
        job_id="job-1",
        lease_id="lease-1",
        lease_epoch=1,
        has_gpu=False,
        rendezvous=_rdzv(rank=0, is_rendezvous_host=True),
    )
    # The rank-0/rendezvous host is named so its endpoint resolves on the network.
    assert host["name"] == "gpuorch-rdzv-abc123-r0"

    worker = build_run_kwargs(
        config=_config(),
        job_spec={**_JOB_SPEC, "world_size": 2},
        job_id="job-1",
        lease_id="lease-2",
        lease_epoch=1,
        has_gpu=False,
        rendezvous=_rdzv(rank=1, is_rendezvous_host=False),
    )
    # A non-host rank joins the same network but takes no reserved name.
    assert worker["network"] == "gpuorch-rdzv-abc123"
    assert "name" not in worker


def test_ddp_preserves_the_adr_007_isolation_profile() -> None:
    kwargs = build_run_kwargs(
        config=_config(),
        job_spec={**_JOB_SPEC, "world_size": 2},
        job_id="job-1",
        lease_id="lease-1",
        lease_epoch=1,
        has_gpu=True,
        rendezvous=_rdzv(world_size=2),
    )
    # Same hard isolation as the single-process path — a user-defined bridge
    # network is still a bridge, never host networking.
    assert kwargs["cap_drop"] == ["ALL"]
    assert kwargs["security_opt"] == ["no-new-privileges"]
    assert kwargs["read_only"] is True
    assert kwargs["privileged"] is False
    assert "cap_add" not in kwargs
    assert kwargs["network"] != "host"
    assert kwargs.get("network_mode") != "host"


def test_world_size_one_rendezvous_stays_single_process() -> None:
    # A world_size=1 rendezvous block must NOT trigger torchrun — the M4
    # single-process path is preserved exactly.
    kwargs = build_run_kwargs(
        config=_config(),
        job_spec=_JOB_SPEC,
        job_id="job-1",
        lease_id="lease-1",
        lease_epoch=1,
        has_gpu=False,
        rendezvous=_rdzv(world_size=1),
    )
    assert "command" not in kwargs
    assert "network" not in kwargs
    assert kwargs["network_mode"] == "bridge"
    assert "DIST_BACKEND" not in kwargs["environment"]


def test_ensure_rendezvous_network_creates_when_absent() -> None:
    client = FakeDockerClient(existing_networks=[])
    ensure_rendezvous_network(client, name="gpuorch-rdzv-abc123")  # type: ignore[arg-type]
    assert client.networks.created == ["gpuorch-rdzv-abc123"]


def test_ensure_rendezvous_network_is_a_noop_when_present() -> None:
    client = FakeDockerClient(existing_networks=["gpuorch-rdzv-abc123"])
    ensure_rendezvous_network(client, name="gpuorch-rdzv-abc123")  # type: ignore[arg-type]
    assert client.networks.created == []


# --- Checkpoints through the orchestrator (ADR-006 addendum 3) ----------------


@pytest.mark.parametrize(
    ("given", "expected"),
    [
        ("http://localhost:8090", "http://host.docker.internal:8090"),
        ("http://127.0.0.1:8090/", "http://host.docker.internal:8090/"),
        ("http://localhost", "http://host.docker.internal"),
        # Anything else names a host the container resolves the same way.
        ("http://192.168.1.10:8090", "http://192.168.1.10:8090"),
        ("https://orchestrator.example.trycloudflare.com",
         "https://orchestrator.example.trycloudflare.com"),
    ],
)
def test_container_reachable_url(given: str, expected: str) -> None:
    from agent.runtime.docker_launcher import container_reachable_url

    assert container_reachable_url(given) == expected


def test_checkpoint_token_reaches_the_trainer_instead_of_bucket_keys() -> None:
    """With a token the trainer goes through the orchestrator, and the S3 keys
    are not passed even when this agent happens to have them configured."""
    kwargs = build_run_kwargs(
        config=_config(
            s3_endpoint_url="http://minio:9000",
            s3_access_key="root",
            s3_secret_key="root-secret",
            s3_bucket_checkpoints="checkpoints",
        ),
        job_spec=_JOB_SPEC,
        job_id="job-1",
        lease_id="lease-1",
        lease_epoch=1,
        has_gpu=False,
        checkpoint_api_url="http://host.docker.internal:8090",
        checkpoint_token="tok",
    )
    env = kwargs["environment"]
    assert env["CHECKPOINT_API_URL"] == "http://host.docker.internal:8090"
    assert env["CHECKPOINT_TOKEN"] == "tok"
    assert "S3_SECRET_KEY" not in env
    assert "S3_ACCESS_KEY" not in env
    # Linux Docker resolves the host alias only with this mapping.
    assert kwargs["extra_hosts"] == {"host.docker.internal": "host-gateway"}


def test_no_host_mapping_for_a_remote_orchestrator() -> None:
    kwargs = build_run_kwargs(
        config=_config(),
        job_spec=_JOB_SPEC,
        job_id="job-1",
        lease_id="lease-1",
        lease_epoch=1,
        has_gpu=False,
        checkpoint_api_url="http://192.168.1.10:8090",
        checkpoint_token="tok",
    )
    assert "extra_hosts" not in kwargs


# --- Trainer image refresh -----------------------------------------------------


class FakeImage:
    def __init__(self, image_id: str) -> None:
        self.id = image_id


class FakeImages:
    def __init__(self, *, local: str | None, remote: str | None) -> None:
        self.local = local
        self.remote = remote
        self.pulled: list[tuple[str, str]] = []

    def get(self, _name: str) -> FakeImage:
        if self.local is None:
            raise docker.errors.ImageNotFound("absent")
        return FakeImage(self.local)

    def pull(self, repository: str, tag: str) -> FakeImage:
        self.pulled.append((repository, tag))
        if self.remote is None:
            raise docker.errors.APIError("registry unreachable")
        self.local = self.remote
        return FakeImage(self.remote)


class FakeImageClient:
    def __init__(self, images: FakeImages) -> None:
        self.images = images


@pytest.mark.parametrize(
    ("image", "published"),
    [
        ("abhisheks1290/gpu-orchestrator-trainer:latest", True),
        ("registry.example.com:5000/team/trainer:v1", True),
        ("gpu-orchestrator-trainer:latest", False),
        ("gpu-orchestrator-trainer", False),
    ],
)
def test_is_published_image(image: str, published: bool) -> None:
    from agent.runtime.docker_launcher import is_published_image

    assert is_published_image(image) is published


def test_refresh_reports_an_update_when_the_registry_has_a_newer_image() -> None:
    from agent.runtime.docker_launcher import refresh_trainer_image

    images = FakeImages(local="sha256:old", remote="sha256:new")
    outcome = refresh_trainer_image(
        FakeImageClient(images), "abhisheks1290/gpu-orchestrator-trainer:latest"  # type: ignore[arg-type]
    )
    assert outcome == "updated"
    assert images.pulled == [("abhisheks1290/gpu-orchestrator-trainer", "latest")]


def test_refresh_fetches_a_missing_image_on_first_start() -> None:
    from agent.runtime.docker_launcher import refresh_trainer_image

    images = FakeImages(local=None, remote="sha256:new")
    assert refresh_trainer_image(FakeImageClient(images), "u/t:latest") == "updated"  # type: ignore[arg-type]


def test_refresh_is_quiet_when_already_current() -> None:
    from agent.runtime.docker_launcher import refresh_trainer_image

    images = FakeImages(local="sha256:same", remote="sha256:same")
    assert refresh_trainer_image(FakeImageClient(images), "u/t:latest") == "current"  # type: ignore[arg-type]


def test_a_failed_refresh_keeps_the_local_image_and_never_raises() -> None:
    from agent.runtime.docker_launcher import refresh_trainer_image

    images = FakeImages(local="sha256:old", remote=None)
    assert refresh_trainer_image(FakeImageClient(images), "u/t:latest") == "failed"  # type: ignore[arg-type]
    assert images.local == "sha256:old"


def test_a_local_only_image_is_never_pulled() -> None:
    from agent.runtime.docker_launcher import refresh_trainer_image

    images = FakeImages(local="sha256:dev", remote="sha256:other")
    outcome = refresh_trainer_image(
        FakeImageClient(images), "gpu-orchestrator-trainer:latest"  # type: ignore[arg-type]
    )
    assert outcome == "skipped"
    assert images.pulled == []
