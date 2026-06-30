"""T40: StorageBackend abstraction unit tests."""
import sys
import os
import pytest
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src", "proxy"))

from storage import (
    GCSStorageBackend,
    LocalFileStorageBackend,
    StorageBackend,
    get_storage_backend,
)


# ─── LocalFileStorageBackend ─────────────────────────────────────────────────

class TestLocalFileStorageBackend:
    def test_write_creates_file(self, tmp_path):
        backend = LocalFileStorageBackend(base_dir=str(tmp_path))
        backend.write("prefix/wf-1/ts.json", '{"k": 1}\n')
        dest = tmp_path / "prefix" / "wf-1" / "ts.json"
        assert dest.exists()
        assert dest.read_text() == '{"k": 1}\n'

    def test_write_creates_nested_dirs(self, tmp_path):
        backend = LocalFileStorageBackend(base_dir=str(tmp_path))
        backend.write("a/b/c/d.json", "{}")
        assert (tmp_path / "a" / "b" / "c" / "d.json").exists()

    def test_base_dir_from_env(self, tmp_path, monkeypatch):
        monkeypatch.setenv("STORAGE_LOCAL_PATH", str(tmp_path))
        backend = LocalFileStorageBackend()
        backend.write("x.json", "data")
        assert (tmp_path / "x.json").exists()

    def test_overwrite_existing_file(self, tmp_path):
        backend = LocalFileStorageBackend(base_dir=str(tmp_path))
        backend.write("f.json", "first")
        backend.write("f.json", "second")
        assert (tmp_path / "f.json").read_text() == "second"

    def test_is_storage_backend_subclass(self):
        assert issubclass(LocalFileStorageBackend, StorageBackend)


# ─── GCSStorageBackend ───────────────────────────────────────────────────────

class TestGCSStorageBackend:
    def test_raises_on_empty_bucket(self):
        with pytest.raises(ValueError, match="bucket"):
            GCSStorageBackend("")

    def test_write_calls_upload(self):
        mock_blob = MagicMock()
        mock_bucket = MagicMock()
        mock_bucket.blob.return_value = mock_blob
        mock_client = MagicMock()
        mock_client.bucket.return_value = mock_bucket
        mock_gcs_module = MagicMock()
        mock_gcs_module.Client.return_value = mock_client

        with patch.dict("sys.modules", {"google.cloud.storage": mock_gcs_module, "google.cloud": MagicMock(storage=mock_gcs_module), "google": MagicMock()}):
            backend = GCSStorageBackend("my-bucket")
            backend.write("prefix/ts.json", '{"x":1}\n')

        mock_gcs_module.Client.assert_called_once()
        mock_client.bucket.assert_called_once_with("my-bucket")
        mock_bucket.blob.assert_called_once_with("prefix/ts.json")
        mock_blob.upload_from_string.assert_called_once_with(
            '{"x":1}\n', content_type="application/json"
        )

    def test_is_storage_backend_subclass(self):
        assert issubclass(GCSStorageBackend, StorageBackend)


# ─── get_storage_backend factory ─────────────────────────────────────────────

class TestGetStorageBackend:
    def test_local_env_returns_local_backend(self, monkeypatch):
        monkeypatch.setenv("STORAGE_BACKEND", "local")
        result = get_storage_backend()
        assert isinstance(result, LocalFileStorageBackend)

    def test_gcs_with_bucket_returns_gcs_backend(self, monkeypatch):
        monkeypatch.setenv("STORAGE_BACKEND", "gcs")
        with patch("storage.GCSStorageBackend") as MockGCS:
            get_storage_backend(bucket="my-bucket")
        MockGCS.assert_called_once_with("my-bucket")

    def test_gcs_without_bucket_returns_none(self, monkeypatch):
        monkeypatch.setenv("STORAGE_BACKEND", "gcs")
        result = get_storage_backend(bucket="")
        assert result is None

    def test_default_env_gcs_without_bucket_returns_none(self, monkeypatch):
        monkeypatch.delenv("STORAGE_BACKEND", raising=False)
        result = get_storage_backend(bucket="")
        assert result is None

    def test_unknown_backend_returns_none(self, monkeypatch):
        monkeypatch.setenv("STORAGE_BACKEND", "s3")
        result = get_storage_backend(bucket="b")
        assert result is None

    def test_local_backend_uses_base_dir_arg(self, monkeypatch, tmp_path):
        monkeypatch.setenv("STORAGE_BACKEND", "local")
        result = get_storage_backend(base_dir=str(tmp_path))
        assert isinstance(result, LocalFileStorageBackend)
        result.write("x.json", "y")
        assert (tmp_path / "x.json").exists()

    def test_gcs_init_failure_returns_none(self, monkeypatch):
        monkeypatch.setenv("STORAGE_BACKEND", "gcs")
        with patch("storage.GCSStorageBackend", side_effect=RuntimeError("no creds")):
            result = get_storage_backend(bucket="b")
        assert result is None
