"""Every ingest leaves a snapshot of the tenant's collection in the snapshot bucket.

Qdrant on Cloud Run keeps its collections in the container's own filesystem, so a new revision
or a restart started it empty. The doc pipeline now writes the changed collection's snapshot to
QDRANT_SNAPSHOT_BUCKET after its upsert, as <collection>.<UTC time>.snapshot, the time taken
before the snapshot is asked for: one asked for later holds everything an earlier one does, so
the newest name is the most complete and the service restores it (infra/qdrant-restore.sh).
Older snapshots of the collection are deleted, never a newer one another ingest wrote.
"""
import importlib.util
import sys
import types
from pathlib import Path

import pytest

_PIPELINE_PATH = Path(__file__).resolve().parents[3] / "src" / "doc-pipeline" / "pipeline.py"
COLLECTION = "rag_nova-stg-01"


def _load(monkeypatch, **env):
    monkeypatch.setenv("TENANT_ID", "NOVA-STG-01")
    monkeypatch.setenv("QDRANT_COLLECTION", COLLECTION)
    monkeypatch.setenv("QDRANT_URL", "https://qdrant.example.run.app")
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    spec = importlib.util.spec_from_file_location("_docpipeline_snapshots", _PIPELINE_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class _Client:
    def __init__(self):
        self.calls = []

    def create_snapshot(self, collection_name, wait=True):
        self.calls.append(("create", collection_name))
        return types.SimpleNamespace(name="rag_nova-stg-01-42-2026-10-03-10-15-00.snapshot")

    def delete_snapshot(self, collection_name, snapshot_name, wait=True):
        self.calls.append(("delete", collection_name, snapshot_name))


class _Blob:
    def __init__(self, bucket, name):
        self.bucket, self.name = bucket, name

    def upload_from_file(self, fileobj, content_type=None):
        if self.bucket.fail_upload:
            raise ConnectionError("storage unreachable")
        self.bucket.objects[self.name] = fileobj.read()

    def delete(self):
        self.bucket.deleted.append(self.name)
        self.bucket.objects.pop(self.name, None)


class _Bucket:
    def __init__(self, objects, fail_upload=False):
        self.objects, self.deleted, self.fail_upload = dict(objects), [], fail_upload

    def blob(self, name):
        return _Blob(self, name)

    def list_blobs(self, prefix=""):
        return [_Blob(self, n) for n in sorted(self.objects) if n.startswith(prefix)]


def _storage(monkeypatch, bucket):
    seen = {}

    class Client:
        def bucket(self, name):
            seen["bucket"] = name
            return bucket

    storage = types.ModuleType("google.cloud.storage")
    storage.Client = Client
    cloud = types.ModuleType("google.cloud")
    cloud.storage = storage
    monkeypatch.setitem(sys.modules, "google.cloud", cloud)
    monkeypatch.setitem(sys.modules, "google.cloud.storage", storage)
    return seen


def _httpx(monkeypatch, body=b"SNAPSHOT-BYTES"):
    import httpx
    seen = {}

    class _Response:
        def raise_for_status(self):
            return None

        def iter_bytes(self):
            yield body[:5]
            yield body[5:]

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    def stream(method, url, headers=None, timeout=None):
        seen.update(method=method, url=url, headers=dict(headers or {}))
        return _Response()

    monkeypatch.setattr(httpx, "stream", stream)
    return seen


_OLDER = f"{COLLECTION}.20261001T000000.000000Z.snapshot"
_NEWER = f"{COLLECTION}.29991231T235959.999999Z.snapshot"     # another ingest's, later
_OTHER = "rag_nova-stg-01_kb.20261001T000000.000000Z.snapshot"  # another collection


def test_an_ingest_leaves_the_newest_snapshot_and_frees_qdrants_copy(monkeypatch):
    mod = _load(monkeypatch, QDRANT_SNAPSHOT_BUCKET="tl-qdrant-snapshots", QDRANT_API_KEY="qk")
    bucket = _Bucket({_OLDER: b"old", _NEWER: b"newer", _OTHER: b"other"})
    seen = _storage(monkeypatch, bucket)
    http = _httpx(monkeypatch)
    monkeypatch.setattr(mod, "_on_gcp", lambda: False)
    client = _Client()

    name = mod.snapshot_collection(client, COLLECTION)

    assert seen["bucket"] == "tl-qdrant-snapshots"
    assert name.startswith(f"{COLLECTION}.2") and name.endswith("Z.snapshot") and _OLDER < name < _NEWER
    assert bucket.objects[name] == b"SNAPSHOT-BYTES"
    assert bucket.deleted == [_OLDER]                      # never another ingest's newer one
    assert set(bucket.objects) == {name, _NEWER, _OTHER}
    assert http["url"] == ("https://qdrant.example.run.app/collections/rag_nova-stg-01/snapshots/"
                           "rag_nova-stg-01-42-2026-10-03-10-15-00.snapshot")
    assert http["headers"] == {"api-key": "qk"}
    assert client.calls == [("create", COLLECTION),
                            ("delete", COLLECTION, "rag_nova-stg-01-42-2026-10-03-10-15-00.snapshot")]


def test_on_gcp_the_download_also_carries_an_identity_token(monkeypatch):
    mod = _load(monkeypatch, QDRANT_SNAPSHOT_BUCKET="b", QDRANT_API_KEY="qk")
    _storage(monkeypatch, _Bucket({}))
    http = _httpx(monkeypatch)
    monkeypatch.setattr(mod, "_on_gcp", lambda: True)
    monkeypatch.setattr(mod, "_gcp_identity_token", lambda audience: f"id-for-{audience}")
    mod.snapshot_collection(_Client(), COLLECTION)
    assert http["headers"] == {"api-key": "qk",
                               "Authorization": "Bearer id-for-https://qdrant.example.run.app"}


def test_the_name_carries_the_time_before_the_snapshot_was_asked_for(monkeypatch):
    # A snapshot asked for after another one holds everything the other does only if its name
    # is taken before the request: then the newest name is always the most complete.
    import datetime as real
    mod = _load(monkeypatch, QDRANT_SNAPSHOT_BUCKET="b")
    _storage(monkeypatch, _Bucket({}))
    _httpx(monkeypatch)
    monkeypatch.setattr(mod, "_on_gcp", lambda: False)
    events = []

    class _Clock(real.datetime):
        @classmethod
        def now(cls, tz=None):
            events.append("clock")
            return real.datetime(2026, 10, 3, 10, 15, 0, 123456, tzinfo=tz)

    class _Recording(_Client):
        def create_snapshot(self, collection_name, wait=True):
            events.append("snapshot")
            return super().create_snapshot(collection_name, wait)

    monkeypatch.setattr(mod, "datetime", _Clock)
    name = mod.snapshot_collection(_Recording(), COLLECTION)
    assert name == f"{COLLECTION}.20261003T101500.123456Z.snapshot"
    assert events == ["clock", "snapshot"]


def test_without_a_bucket_nothing_is_written(monkeypatch):
    mod = _load(monkeypatch)
    monkeypatch.delenv("QDRANT_SNAPSHOT_BUCKET", raising=False)
    client = _Client()
    assert mod.snapshot_collection(client, COLLECTION) is None
    assert client.calls == []


def test_a_snapshot_that_cannot_be_stored_fails_the_ingest_and_frees_qdrants_copy(monkeypatch):
    # The document is in Qdrant but would not survive a restart: the job must not report success.
    mod = _load(monkeypatch, QDRANT_SNAPSHOT_BUCKET="b")
    _storage(monkeypatch, _Bucket({}, fail_upload=True))
    _httpx(monkeypatch)
    monkeypatch.setattr(mod, "_on_gcp", lambda: False)
    client = _Client()
    with pytest.raises(ConnectionError):
        mod.snapshot_collection(client, COLLECTION)
    assert client.calls[-1][0] == "delete"


def test_the_upsert_is_followed_by_a_snapshot(monkeypatch):
    mod = _load(monkeypatch, QDRANT_SNAPSHOT_BUCKET="b")
    order = []

    class _Qdrant:
        def __init__(self, **kwargs):
            pass

        def get_collections(self):
            return types.SimpleNamespace(collections=[])

        def create_collection(self, **kwargs):
            order.append("create_collection")

        def upsert(self, collection_name, points):
            order.append("upsert")

    qdrant_client = types.ModuleType("qdrant_client")
    qdrant_client.QdrantClient = _Qdrant
    models = types.ModuleType("qdrant_client.models")
    models.PointStruct = models.VectorParams = lambda **kw: kw
    models.SparseVectorParams = models.SparseIndexParams = lambda **kw: kw
    models.Distance = types.SimpleNamespace(COSINE="Cosine")
    qdrant_client.models = models
    monkeypatch.setitem(sys.modules, "qdrant_client", qdrant_client)
    monkeypatch.setitem(sys.modules, "qdrant_client.models", models)
    monkeypatch.setattr(mod, "_qdrant_client_kwargs", lambda url: {})
    monkeypatch.setattr(mod, "_to_sparse_vector", lambda x: x)
    monkeypatch.setattr(mod, "snapshot_collection",
                        lambda client, collection: order.append(("snapshot", collection)))
    mod.upsert_to_qdrant(["chunk"], [[0.1, 0.2]], [None], "gs://b/doc.pdf")
    assert order == ["create_collection", "upsert", ("snapshot", COLLECTION)]
