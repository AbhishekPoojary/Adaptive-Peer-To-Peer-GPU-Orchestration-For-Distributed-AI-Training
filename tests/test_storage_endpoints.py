"""The orchestrator's storage traffic and peers' dataset links use different
endpoints.

Before the split, ``S3_ENDPOINT_URL`` had to be this host's LAN address so that
signed dataset URLs worked on other machines -- which meant the orchestrator's
own uploads and checkpoint I/O went through that address too. When the
laptop's IP changed, every checkpoint write failed with 503 and training
stalled on each one. Signing is offline computation, so no network is needed
to test which host a URL names.
"""

from __future__ import annotations

from urllib.parse import urlsplit

from orchestrator.core.config import Settings
from orchestrator.services.object_store import DatasetObjectStore


def _settings(**overrides: object) -> Settings:
    base: dict[str, object] = {"s3_endpoint_url": "http://minio:9000"}
    base.update(overrides)
    return Settings(**base)  # type: ignore[arg-type]


def test_dataset_links_are_signed_for_the_public_endpoint() -> None:
    store = DatasetObjectStore(_settings(s3_public_endpoint_url="http://10.0.0.5:9010"))
    url = store.presigned_get_url(key="datasets/a.zip", expires_seconds=60)
    assert urlsplit(url).netloc == "10.0.0.5:9010"
    # ...while the orchestrator's own client stays on the internal name.
    assert store._get_client().meta.endpoint_url == "http://minio:9000"


def test_unset_or_blank_public_endpoint_falls_back_to_the_internal_one() -> None:
    for value in (None, "", "   "):
        store = DatasetObjectStore(_settings(s3_public_endpoint_url=value))
        url = store.presigned_get_url(key="datasets/a.zip", expires_seconds=60)
        assert urlsplit(url).netloc == "minio:9000", value
