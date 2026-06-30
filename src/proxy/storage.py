"""
Storage backend abstraction for G18 JSONL export.

Select backend via STORAGE_BACKEND env var:
  STORAGE_BACKEND=gcs   (default) — uploads to GCS; requires jsonl_gcs_bucket config
  STORAGE_BACKEND=local           — writes to local filesystem; no GCP credentials needed

Local path root: STORAGE_LOCAL_PATH env var (default: ./token-usage-logs)
"""
import abc
import logging
import os
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)


class StorageBackend(abc.ABC):
    """Write a single JSON record to persistent storage."""

    @abc.abstractmethod
    def write(self, path: str, data: str) -> None:
        """Write `data` (JSON string) to `path`.

        Args:
            path: Provider-relative path, e.g. ``prefix/workflow-id/ts.json``.
            data: UTF-8 JSON string to persist.
        """


class GCSStorageBackend(StorageBackend):
    """Write records to a GCS bucket using google-cloud-storage."""

    def __init__(self, bucket: str) -> None:
        if not bucket:
            raise ValueError("GCSStorageBackend: bucket name must not be empty")
        self._bucket = bucket

    def write(self, path: str, data: str) -> None:
        from google.cloud import storage  # lazy: optional in non-GCP envs
        client = storage.Client()
        blob = client.bucket(self._bucket).blob(path)
        blob.upload_from_string(data, content_type="application/json")
        logger.debug("GCSStorageBackend: uploaded gs://%s/%s", self._bucket, path)


class LocalFileStorageBackend(StorageBackend):
    """Write records to the local filesystem — useful for local dev / CI."""

    def __init__(self, base_dir: Optional[str] = None) -> None:
        self._base = Path(base_dir or os.environ.get("STORAGE_LOCAL_PATH", "token-usage-logs"))

    def write(self, path: str, data: str) -> None:
        dest = self._base / path
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(data, encoding="utf-8")
        logger.debug("LocalFileStorageBackend: wrote %s", dest)


def get_storage_backend(bucket: str = "", base_dir: Optional[str] = None) -> Optional[StorageBackend]:
    """Return the configured backend, or None if export is disabled.

    Resolution order:
    1. ``STORAGE_BACKEND=local``  → ``LocalFileStorageBackend``
    2. ``STORAGE_BACKEND=gcs`` (or unset) + ``bucket`` non-empty → ``GCSStorageBackend``
    3. Otherwise → ``None`` (export disabled; caller is a no-op)
    """
    backend_name = os.environ.get("STORAGE_BACKEND", "gcs").lower().strip()

    if backend_name == "local":
        return LocalFileStorageBackend(base_dir)

    if backend_name == "gcs":
        if not bucket:
            return None  # GCS requested but no bucket configured — disable silently
        try:
            return GCSStorageBackend(bucket)
        except Exception as exc:
            logger.warning("get_storage_backend: GCSStorageBackend init failed: %s", exc)
            return None

    logger.warning("get_storage_backend: unknown STORAGE_BACKEND=%r, export disabled", backend_name)
    return None
