"""Object storage access for the orchestrator (ADR-014).

The orchestrator had no S3 code before this: checkpoints are written by the
*trainer*, which carries its own client in ``trainer/checkpoint.py``. Datasets
are the first thing the control plane itself has to store, so this is a small,
deliberate surface rather than a general-purpose wrapper — upload a file, delete
an object, and hand out a time-limited URL to read one.

``boto3`` is imported lazily inside the client factory, matching what
``trainer/checkpoint.py`` already does. That keeps ``import orchestrator`` free
of a heavyweight dependency for every code path that never touches storage,
including the entire test suite outside these few endpoints.

Uploads stream from a path on disk. The archive has already been written to a
temporary file during validation, and re-reading it into memory to send would
mean holding a multi-gigabyte upload in RAM for no reason.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # pragma: no cover - typing only
    from orchestrator.core.config import Settings

logger = logging.getLogger("orchestrator.object_store")


class ObjectStoreError(Exception):
    """Object storage could not complete the request.

    Distinct from any validation error: this means the *storage* failed, which is
    an operational problem, not something the uploader can fix by sending a
    different file.
    """


class DatasetObjectStore:
    """S3/MinIO operations for the datasets bucket."""

    def __init__(self, settings: Settings) -> None:
        self._bucket = settings.s3_bucket_datasets
        self._endpoint = settings.s3_endpoint_url
        self._access_key = settings.s3_access_key
        self._secret_key = settings.s3_secret_key
        self._region = settings.s3_region
        self._client: Any | None = None

    def _get_client(self) -> Any:
        if self._client is not None:
            return self._client
        try:
            import boto3
            from botocore.client import Config
        except ImportError as exc:  # pragma: no cover - dependency is pinned
            raise ObjectStoreError(
                "boto3 is not installed; dataset storage is unavailable"
            ) from exc

        self._client = boto3.client(
            "s3",
            endpoint_url=self._endpoint,
            aws_access_key_id=self._access_key,
            aws_secret_access_key=self._secret_key,
            region_name=self._region,
            # MinIO requires path-style addressing: virtual-host style would
            # resolve "bucket.localhost", which does not exist.
            config=Config(signature_version="s3v4", s3={"addressing_style": "path"}),
        )
        return self._client

    def ensure_bucket(self) -> None:
        """Create the datasets bucket if it is missing.

        Called before the first upload rather than at startup, so an orchestrator
        with no object storage configured still boots and serves every endpoint
        that does not need it.
        """
        import botocore.exceptions

        client = self._get_client()
        try:
            client.head_bucket(Bucket=self._bucket)
        except botocore.exceptions.ClientError:
            try:
                client.create_bucket(Bucket=self._bucket)
            except botocore.exceptions.ClientError as exc:
                # A concurrent upload may have won the race; that is success.
                code = exc.response.get("Error", {}).get("Code", "")
                if code not in ("BucketAlreadyOwnedByYou", "BucketAlreadyExists"):
                    raise ObjectStoreError(
                        f"could not create the {self._bucket!r} bucket: {exc}"
                    ) from exc
        except botocore.exceptions.EndpointConnectionError as exc:
            raise ObjectStoreError(
                f"object storage at {self._endpoint} is unreachable"
            ) from exc

    def upload_file(self, *, local_path: str, key: str) -> None:
        """Stream a local file into the datasets bucket under ``key``."""
        import botocore.exceptions

        client = self._get_client()
        try:
            client.upload_file(local_path, self._bucket, key)
        except (botocore.exceptions.BotoCoreError, botocore.exceptions.ClientError) as exc:
            raise ObjectStoreError(f"could not store the dataset: {exc}") from exc

    def delete_object(self, *, key: str) -> None:
        """Remove an object. A missing object is not an error.

        Deletion is best-effort by design: the caller has already committed the
        row's ``deleted_at``, and a storage hiccup must not resurrect a dataset
        the operator has said should be gone.
        """
        import botocore.exceptions

        client = self._get_client()
        try:
            client.delete_object(Bucket=self._bucket, Key=key)
        except (botocore.exceptions.BotoCoreError, botocore.exceptions.ClientError) as exc:
            logger.warning("could not delete dataset object %s: %s", key, exc)

    def presigned_get_url(self, *, key: str, expires_seconds: int) -> str:
        """Return a time-limited URL a peer can GET without credentials.

        Used to hand a peer its dataset without giving every node the bucket's
        keys. The URL is minted per lease and expires, so a peer that has
        finished a job cannot keep reading the data indefinitely.
        """
        import botocore.exceptions

        client = self._get_client()
        try:
            url: str = client.generate_presigned_url(
                "get_object",
                Params={"Bucket": self._bucket, "Key": key},
                ExpiresIn=expires_seconds,
            )
        except (botocore.exceptions.BotoCoreError, botocore.exceptions.ClientError) as exc:
            raise ObjectStoreError(f"could not sign a dataset URL: {exc}") from exc
        return url
