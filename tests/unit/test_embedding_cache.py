"""Phase 4 - tenant-scoped, content-addressed embedding reuse.

Artefacts in one tenant already shared a vector *collection*, but every ingest re-embedded
identical content from scratch and no embedding cache existed anywhere (`ml_models` caches
model objects, never vectors). These tests pin the three properties that make sharing safe:

  1. a cache hit returns a BYTE-IDENTICAL vector, so retrieval results cannot shift;
  2. identical text encodes exactly once, however many callers ask for it;
  3. one tenant's cache is invisible to another, like every other key in the proxy.
"""
import sys, os
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "src", "proxy")))

from unittest.mock import patch

import pytest

import embedding_cache
from embedding_cache import _pack, _unpack, content_hash, embed_cached


class _FakeRedis:
    def __init__(self):
        self.data = {}

    async def get(self, key):
        return self.data.get(key)

    async def set(self, key, value, ex=None):
        self.data[key] = value


class _CountingModel:
    """Stands in for sentence-transformers; counts how often encode() actually runs."""

    def __init__(self):
        self.calls = 0

    def encode(self, text):
        self.calls += 1
        import numpy as np
        # float32 is what the real models emit, which is what makes the packed round trip
        # exact rather than merely close.
        rng = np.random.default_rng(abs(hash(text)) % (2**32))
        return rng.random(8, dtype=np.float32)


@pytest.fixture
def fake_redis(monkeypatch):
    r = _FakeRedis()
    monkeypatch.setattr("cache.redis_pool.get_redis", lambda: r)
    embedding_cache._local.clear()
    return r


@pytest.fixture
def model(monkeypatch):
    m = _CountingModel()
    monkeypatch.setattr("ml_models.get_sentence_transformer", lambda name: m)
    return m


@pytest.fixture
def no_redis(monkeypatch):
    def _boom():
        raise ConnectionError("no redis")
    monkeypatch.setattr("cache.redis_pool.get_redis", _boom)
    embedding_cache._local.clear()


class TestVectorRoundTripIsExact:
    """The quality gate: a cached vector must be indistinguishable from a fresh one.

    Vectors are struct-packed float32 rather than JSON (a 384-dim vector is ~1.5 KB packed
    against ~8 KB as text). float32 is what the model produced, so packing is lossless -
    if it were not, cached and uncached retrieval could rank differently.
    """

    def test_pack_unpack_is_byte_identical(self):
        vector = [0.1, -0.25, 3.5, 0.0, 1e-7, -1.0, 0.333333, 2.5]
        import struct
        expected = list(struct.unpack("8f", struct.pack("8f", *vector)))
        assert _unpack(_pack(vector)) == expected

    async def test_cache_hit_matches_a_fresh_encode(self, fake_redis, model):
        first = await embed_cached("hello world", "m1")
        embedding_cache._local.clear()          # force the Redis path, not the local one
        second = await embed_cached("hello world", "m1")
        assert second == first, "a cached vector must be byte-identical to a fresh one"


class TestIdenticalContentEncodesOnce:
    async def test_repeat_text_hits_the_cache(self, fake_redis, model):
        for _ in range(5):
            await embed_cached("same text", "m1")
        assert model.calls == 1, f"encoded {model.calls} times; identical text must encode once"

    async def test_second_artefact_reuses_the_first_vector(self, fake_redis, model):
        """Two apps in one tenant indexing the same document pay for it once."""
        await embed_cached("shared document", "m1", prefix="t:acme:")
        embedding_cache._local.clear()          # a different process / worker
        await embed_cached("shared document", "m1", prefix="t:acme:")
        assert model.calls == 1

    async def test_different_text_still_encodes(self, fake_redis, model):
        await embed_cached("one", "m1")
        await embed_cached("two", "m1")
        assert model.calls == 2

    async def test_different_model_is_a_different_key(self, fake_redis, model):
        """A vector from one model must never be served for another."""
        await embed_cached("same text", "m1")
        await embed_cached("same text", "m2")
        assert model.calls == 2


class TestTenantIsolation:
    async def test_one_tenant_cannot_read_anothers_vectors(self, fake_redis, model):
        await embed_cached("confidential", "m1", prefix="t:acme:")
        embedding_cache._local.clear()
        await embed_cached("confidential", "m1", prefix="t:globex:")
        assert model.calls == 2, "globex reused acme's cached vector"
        assert len(fake_redis.data) == 2

    async def test_keys_carry_the_tenant_prefix(self, fake_redis, model):
        await embed_cached("x", "m1", prefix="t:acme:")
        assert fake_redis.data, "nothing was cached, so the key shape proves nothing"
        assert all(k.startswith("t:acme:emb:") for k in fake_redis.data)


class TestDegradesSafely:
    async def test_redis_down_still_returns_a_vector(self, no_redis, model):
        """A cache outage must slow things down, never change a result."""
        vector = await embed_cached("text", "m1")
        assert len(vector) == 8
        assert model.calls == 1

    async def test_redis_down_still_uses_the_local_cache(self, no_redis, model):
        await embed_cached("text", "m1")
        await embed_cached("text", "m1")
        assert model.calls == 1, "the in-process cache should still absorb the repeat"

    async def test_local_cache_is_bounded(self, no_redis, model):
        from embedding_cache import _LOCAL_MAX, _local_put
        for i in range(_LOCAL_MAX + 50):
            _local_put(f"k{i}", [0.0], 3600)
        assert len(embedding_cache._local) <= _LOCAL_MAX


class TestContentHash:
    def test_is_stable_and_content_addressed(self):
        assert content_hash("abc") == content_hash("abc")
        assert content_hash("abc") != content_hash("abd")
        assert len(content_hash("abc")) == 64


class TestIngestSkipsUnchangedContent:
    """Re-ingesting a corpus used to re-encode every chunk unconditionally.

    `add_chunk` embedded before it looked at the row, so an unchanged document cost a full
    encode on every run - and a second app indexing the same document paid it again.
    """

    class _Conn:
        def __init__(self, stored_hash=None):
            self.stored_hash = stored_hash
            self.executed = []

        async def fetchval(self, sql, *args):
            return self.stored_hash

        async def execute(self, sql, *args):
            self.executed.append((sql, args))

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

    class _Pool:
        def __init__(self, conn):
            self._conn = conn

        def acquire(self):
            return self._conn

    async def _add(self, conn, text, model):
        from middleware.g07_pgvector_fallback import PGVectorRAG
        rag = PGVectorRAG(dsn="postgres://x")
        with patch.object(PGVectorRAG, "_get_pool", return_value=self._Pool(conn)):
            return await rag.add_chunk("chunk-1", text)

    async def test_unchanged_content_skips_embed_and_write(self, fake_redis, model):
        from embedding_cache import content_hash as ch
        text = "a stable paragraph of documentation"
        conn = self._Conn(stored_hash=ch(text))
        assert await self._add(conn, text, model) is True
        assert model.calls == 0, "unchanged content must not be re-embedded"
        assert conn.executed == [], "unchanged content must not be re-written"

    async def test_changed_content_is_re_embedded(self, fake_redis, model):
        conn = self._Conn(stored_hash="stale-hash-from-an-older-revision")
        assert await self._add(conn, "new text", model) is True
        assert model.calls == 1
        assert conn.executed, "changed content must be written"

    async def test_new_chunk_is_embedded_and_stores_its_hash(self, fake_redis, model):
        from embedding_cache import content_hash as ch
        text = "brand new chunk"
        conn = self._Conn(stored_hash=None)
        assert await self._add(conn, text, model) is True
        assert model.calls == 1
        sql, args = conn.executed[0]
        assert "content_hash" in sql
        assert ch(text) in args, "the delta-sync key must be persisted or dedup never fires"
