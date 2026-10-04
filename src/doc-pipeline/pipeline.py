"""
G03 · Document Ingestion Pipeline — Cloud Run Job
Triggered when a document is uploaded to GCS.
Steps: download → extract text (Unstructured) → strip boilerplate
       → chunk (256-512 tokens) → embed dense + sparse (SPLADE/BM25)
       → upsert to Qdrant named vectors → snapshot the collection to QDRANT_SNAPSHOT_BUCKET.
"""
import logging
import os
import re
import sys
import tempfile
import threading
import time
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)
logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"), stream=sys.stdout)

_GCS_BUCKET = os.getenv("GCS_BUCKET", "")
_GCS_OBJECT = os.getenv("GCS_OBJECT", "")
_QDRANT_URL = os.getenv("QDRANT_URL", "http://localhost:6333")
_QDRANT_COLLECTION = os.getenv("QDRANT_COLLECTION", "rag_docs")
# Tenant scoping: injected per-run as a container override alongside QDRANT_COLLECTION
# (see g03_doc_pipeline.trigger_doc_ingestion). Every upserted point is stamped with it
# for defense-in-depth even though the collection already segregates by tenant.
_TENANT_ID = os.getenv("TENANT_ID", "default")
_CHUNK_SIZE = int(os.getenv("CHUNK_SIZE_TOKENS", "400"))
_CHUNK_OVERLAP = int(os.getenv("CHUNK_OVERLAP_TOKENS", "50"))
_SPARSE_MODEL = os.getenv("SPARSE_EMBEDDING_MODEL", "Qdrant/bm25")

# Collection allowlist: refuse to create/write a collection whose name isn't a safe
# identifier, so a malformed TENANT_ID can't spray a garbage collection or inject. This
# matches what TenantContext.for_tenant actually produces — rag_<sanitised>, where the
# sanitised id may contain uppercase and hyphens (sanitise_tenant_id allows [A-Za-z0-9_-]).
# Kept deliberately broad enough to accept every real rag_<tenant> yet reject whitespace,
# ';', quotes, and other injection characters.
_VALID_COLLECTION_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_-]{0,62}$")

# Fixed namespace for deterministic, tenant-scoped point ids (uuid5). Replaces the old
# 32-bit md5-truncation which could silently overwrite another doc's/tenant's chunk.
_POINT_ID_NAMESPACE = uuid.UUID("6f9619ff-8b86-d011-b42d-00c04fc964ff")


def _now_iso() -> str:
    """Ingest timestamp (UTC, ISO-8601). Wrapped so tests can inject a fixed 'now'."""
    return datetime.now(timezone.utc).isoformat()


def _source_date() -> Optional[str]:
    """The document's own authored/effective date, if the operator supplied one via
    the ``SOURCE_DATE`` env override (ISO-8601). Returns the normalised ISO string, or
    ``None`` when absent/unparseable — freshness then falls back to ``ingested_at``."""
    raw = os.getenv("SOURCE_DATE", "").strip()
    if not raw:
        return None
    try:
        # Accept a plain date or a full timestamp; normalise to ISO. 'Z' → +00:00.
        return datetime.fromisoformat(raw.replace("Z", "+00:00")).isoformat()
    except Exception:
        logger.warning("SOURCE_DATE=%r is not ISO-8601 — ignoring (falling back to ingested_at)", raw)
        return None


def download_from_gcs(bucket: str, obj: str) -> bytes:
    from google.cloud import storage
    client = storage.Client()
    blob = client.bucket(bucket).blob(obj)
    return blob.download_as_bytes()


def _cloud_run_auth_headers(url: str) -> dict:
    """``Authorization: Bearer <identity token>`` for an IAM-protected Cloud Run service
    (tika-svc on GCP), from the metadata server; ``{}`` for any other URL. The audience is
    the service origin, and only ``https://*.run.app`` hosts get a token. This job image
    does not ship the proxy's ``ml_models``, whose helper this mirrors."""
    import urllib.request
    from urllib.parse import urlsplit

    parts = urlsplit(url)
    host = (parts.hostname or "").lower()
    if parts.scheme != "https" or not host.endswith(".run.app"):
        return {}
    req = urllib.request.Request(
        "http://metadata.google.internal/computeMetadata/v1/instance/"
        f"service-accounts/default/identity?audience=https://{host}",
        headers={"Metadata-Flavor": "Google"},
    )
    # The fixed metadata-server URL, never caller input (bandit B310).
    with urllib.request.urlopen(req, timeout=5) as resp:  # nosec B310
        token = resp.read().decode().strip()
    return {"Authorization": f"Bearer {token}"}


# ─── Qdrant connection: the proxy's settings (ml_models.qdrant_client_kwargs) ────
# This image does not ship the proxy's ml_models, so this is a copy, and a test holds it to
# the proxy's. On GCP, Qdrant runs on Cloud Run behind IAM plus its own API key: without the
# key, an identity token and port 443, every call from this job failed.
_id_token_cache: Dict[str, Tuple[str, float]] = {}
_id_token_lock = threading.Lock()
# Cloud Run names itself in every service's (K_SERVICE) and job's (CLOUD_RUN_JOB)
# environment. Elsewhere the metadata server is probed; a "no" is kept _GCP_NO_TTL_S only.
_CLOUD_RUN_ENV = ("K_SERVICE", "CLOUD_RUN_JOB")
_GCP_NO_TTL_S = 300.0
_gcp_metadata_available: Optional[bool] = None
_gcp_checked_at = 0.0
_clock = time.monotonic


def _gcp_identity_token(audience: str) -> str:
    """A GCP identity token for ``audience`` from the metadata server, cached ~50 min."""
    import urllib.request

    now = time.time()
    tok, exp = _id_token_cache.get(audience, ("", 0.0))
    if tok and now < exp:
        return tok
    with _id_token_lock:
        tok, exp = _id_token_cache.get(audience, ("", 0.0))
        if tok and now < exp:
            return tok
        req = urllib.request.Request(
            "http://metadata.google.internal/computeMetadata/v1/instance/"
            f"service-accounts/default/identity?audience={audience}",
            headers={"Metadata-Flavor": "Google"},
        )
        # The fixed metadata-server URL, never caller input (bandit B310).
        token = urllib.request.urlopen(req, timeout=5).read().decode().strip()  # nosec B310
        _id_token_cache[audience] = (token, now + 3000)  # refresh 10 min before 1 h expiry
        return token


def _on_gcp() -> bool:
    """Whether this process runs on GCP: Cloud Run's environment says so, else the metadata
    server answers. A yes is kept for the process, a no for _GCP_NO_TTL_S only (one slow
    probe used to turn the identity token off for the whole run)."""
    global _gcp_metadata_available, _gcp_checked_at
    if _gcp_metadata_available:
        return True
    if any(os.environ.get(name) for name in _CLOUD_RUN_ENV):
        _gcp_metadata_available = True
        return True
    now = _clock()
    if _gcp_metadata_available is False and now - _gcp_checked_at < _GCP_NO_TTL_S:
        return False
    import urllib.request
    try:
        req = urllib.request.Request(
            "http://metadata.google.internal/computeMetadata/v1/instance/id",
            headers={"Metadata-Flavor": "Google"},
        )
        urllib.request.urlopen(req, timeout=2).read()  # nosec B310 (fixed URL)
        _gcp_metadata_available = True
    except Exception:
        _gcp_metadata_available = False
        _gcp_checked_at = now
    return _gcp_metadata_available


def _qdrant_client_kwargs(url: str) -> Dict[str, Any]:
    """QdrantClient kwargs for ``url``: the Qdrant API key (``QDRANT_API_KEY``), port 443 for
    an https URL without a port (the client defaults to 6333, which Cloud Run does not
    serve), and on GCP a Cloud Run identity token for the IAM layer."""
    kwargs: Dict[str, Any] = {"url": url, "api_key": os.getenv("QDRANT_API_KEY") or None}
    try:
        import inspect
        from qdrant_client import QdrantClient
        if "check_compatibility" in inspect.signature(QdrantClient.__init__).parameters:
            kwargs["check_compatibility"] = False
    except Exception as exc:
        logger.debug("qdrant-client compatibility probe failed: %r", exc)
    if url.startswith("https://") and ":" not in url.split("//", 1)[1].split("/", 1)[0]:
        kwargs["port"] = 443
    if url.startswith("https://") and os.getenv("QDRANT_LOCAL_NOAUTH") != "1" and _on_gcp():
        kwargs["auth_token_provider"] = lambda: _gcp_identity_token(url)
    return kwargs


def extract_text_with_tika(content: bytes, filename: str) -> str:
    """Use Apache Tika sidecar to extract text from documents."""
    try:
        import httpx

        tika_url = os.getenv("TIKA_SIDECAR_URL", "http://tika-svc:9998")

        with httpx.Client(timeout=30.0) as client:
            resp = client.put(
                f"{tika_url}/tika",
                content=content,
                headers={
                    "Content-Type": "application/octet-stream",
                    "Accept": "text/plain",
                    "X-Filename": filename,
                    **_cloud_run_auth_headers(tika_url),
                },
            )
            resp.raise_for_status()
            return resp.text
    except Exception as exc:
        logger.debug("Tika extraction failed: %s", exc)
        return ""


# Formats whose bytes may be stored as text when no parser reads them, and only when those
# bytes really are UTF-8 with no NUL.
_TEXT_SUFFIXES = frozenset({".txt", ".text", ".md", ".markdown", ".rst", ".csv", ".tsv",
                            ".json", ".jsonl", ".yaml", ".yml", ".xml", ".html", ".htm",
                            ".log"})


class UnreadableDocumentError(RuntimeError):
    """Neither Unstructured nor Tika could read the file, and it is not plain UTF-8 text.
    Storing its bytes as text would put noise into the tenant's RAG context, so the job
    stores nothing instead."""


def _extract_with_unstructured(content: bytes, filename: str) -> str:
    try:
        import tempfile
        from unstructured.partition.auto import partition

        with tempfile.NamedTemporaryFile(suffix=os.path.splitext(filename)[1], delete=False) as f:
            f.write(content)
            tmp_path = f.name
        try:
            elements = partition(filename=tmp_path)
        finally:
            os.unlink(tmp_path)
        return "\n\n".join(str(e) for e in elements)
    except Exception as exc:
        logger.warning("Unstructured could not read %s: %s", filename, exc)
        return ""


def _as_text(content: bytes, filename: str) -> Optional[str]:
    """``content`` as text when ``filename`` is a plain-text format and the bytes are UTF-8
    with no NUL; otherwise None."""
    if os.path.splitext(filename)[1].lower() not in _TEXT_SUFFIXES or b"\x00" in content:
        return None
    try:
        return content.decode("utf-8")
    except UnicodeDecodeError:
        return None


def extract_text(content: bytes, filename: str) -> str:
    """Text of a document: Unstructured first, so PDF and Word read as before; then the Tika
    sidecar, when TIKA_SIDECAR_URL is set, for what Unstructured cannot read (Excel,
    PowerPoint and more); then a plain-text file as UTF-8. Raises UnreadableDocumentError
    for anything else, rather than storing its bytes as text."""
    text = _extract_with_unstructured(content, filename)
    if text.strip():
        return text
    if os.getenv("TIKA_SIDECAR_URL"):
        text = extract_text_with_tika(content, filename)
        if text.strip():
            logger.info("Extracted %s with the Tika sidecar", filename)
            return text
    text = _as_text(content, filename)
    if text is not None:
        return text
    raise UnreadableDocumentError(f"no reader could extract text from {filename}")


def strip_boilerplate(text: str) -> str:
    """Remove common boilerplate: headers/footers, legal notices, base64 blobs."""
    import re
    # Remove base64 blobs
    text = re.sub(r"[A-Za-z0-9+/]{100,}={0,2}", "[base64-removed]", text)
    # Collapse multiple blank lines
    text = re.sub(r"\n{3,}", "\n\n", text)
    # Strip HTML tags if any residual
    text = re.sub(r"<[^>]+>", " ", text)
    # Remove page numbers
    text = re.sub(r"\bPage\s+\d+\s+of\s+\d+\b", "", text, flags=re.IGNORECASE)
    return text.strip()


def chunk_text(text: str, chunk_size: int = _CHUNK_SIZE, overlap: int = _CHUNK_OVERLAP) -> List[str]:
    """Split text into overlapping token-budget chunks."""
    try:
        from langchain_text_splitters import RecursiveCharacterTextSplitter
        # Approximate: 1 token ≈ 4 chars
        splitter = RecursiveCharacterTextSplitter(
            chunk_size=chunk_size * 4,
            chunk_overlap=overlap * 4,
            separators=["\n\n", "\n", ". ", " ", ""],
        )
        return splitter.split_text(text)
    except Exception as exc:
        logger.warning("Chunking fallback: %s", exc)
        char_size = chunk_size * 4
        return [text[i:i + char_size] for i in range(0, len(text), char_size - overlap * 4)]


def table_to_csv(text: str) -> str:
    """
    Convert markdown-style tables in the extracted text to compact CSV rows.
    Reduces token cost of table-heavy documents ~40-60%.
    """
    import re
    lines = text.split("\n")
    out: List[str] = []
    i = 0
    while i < len(lines):
        line = lines[i]
        # Detect markdown table rows: | col | col |
        if "|" in line and line.strip().startswith("|"):
            cells = [c.strip() for c in line.strip().strip("|").split("|")]
            # Skip separator rows (e.g. |---|---|)
            if all(re.match(r'^[-:]+$', c) for c in cells if c):
                i += 1
                continue
            out.append(",".join(cells))
        else:
            out.append(line)
        i += 1
    return "\n".join(out)


def _token_counter():
    try:
        import tiktoken
        enc = tiktoken.get_encoding("cl100k_base")
        return lambda text: len(enc.encode(text))
    except Exception:
        return lambda text: len(text) // 4


def _cut_to_budget(text: str, max_tokens: int, count) -> List[str]:
    """Cut ``text`` into consecutive pieces of at most ``max_tokens`` tokens, each ending at a
    line break, else a space, when one falls in the piece's last three quarters (so no piece
    is shrunk below a quarter of the budget just to end on a boundary)."""
    pieces: List[str] = []
    while text and count(text) > max_tokens:
        lo, hi = 1, len(text)           # the longest prefix within the budget
        while lo < hi:
            mid = (lo + hi + 1) // 2
            if count(text[:mid]) <= max_tokens:
                lo = mid
            else:
                hi = mid - 1
        cut = lo
        for boundary in ("\n", " "):
            at = text.rfind(boundary, 0, lo + 1)
            if at > lo // 4:
                cut = at
                break
        pieces.append(text[:cut])
        text = text[cut:].lstrip()
    if text:
        pieces.append(text)
    return pieces


def split_oversized_chunks(chunks: List[str], max_tokens: int = 4000) -> List[str]:
    """Cut any chunk over ``max_tokens`` into consecutive pieces within it.

    The chunker cuts by characters, about four per token, so a chunk only runs over when its
    text is token-dense (some non-Latin scripts) or CHUNK_SIZE_TOKENS is raised. Cutting
    keeps every word and needs no model, no key, and nothing leaves the job."""
    count = _token_counter()
    max_tokens = max(1, int(max_tokens))
    result: List[str] = []
    for chunk in chunks:
        if count(chunk) <= max_tokens:
            result.append(chunk)
        else:
            pieces = _cut_to_budget(chunk, max_tokens, count)
            logger.info("Cut an oversized chunk into %d pieces of at most %d tokens",
                        len(pieces), max_tokens)
            result.extend(pieces)
    return result


def embed_chunks_dense(chunks: List[str]) -> List[List[float]]:
    from fastembed import TextEmbedding
    model = TextEmbedding("sentence-transformers/all-MiniLM-L6-v2")
    return [emb.tolist() for emb in model.embed(chunks)]


def embed_chunks_sparse(chunks: List[str]) -> List:
    """Generate SPLADE sparse vectors via fastembed (CPU, OSS)."""
    from fastembed import SparseTextEmbedding
    model = SparseTextEmbedding(_SPARSE_MODEL)
    return list(model.embed(chunks))


def _to_sparse_vector(sparse_emb) -> object:
    """Convert fastembed SparseEmbedding → Qdrant SparseVector."""
    from qdrant_client.models import SparseVector
    return SparseVector(
        indices=sparse_emb.indices.tolist(),
        values=sparse_emb.values.tolist(),
    )


def upsert_to_qdrant(
    chunks: List[str],
    dense_embeddings: List[List[float]],
    sparse_embeddings: List,
    source: str,
) -> None:
    from qdrant_client import QdrantClient
    from qdrant_client.models import (
        Distance, PointStruct, VectorParams,
        SparseVectorParams, SparseIndexParams,
    )

    if not _VALID_COLLECTION_RE.match(_QDRANT_COLLECTION):
        logger.error(
            "Refusing to upsert: QDRANT_COLLECTION %r is not a valid identifier "
            "(^[A-Za-z][A-Za-z0-9_-]{0,62}$). Check the TENANT_ID override.",
            _QDRANT_COLLECTION,
        )
        sys.exit(1)

    client = QdrantClient(**_qdrant_client_kwargs(_QDRANT_URL))

    # Collection management: ensure named vectors (dense + sparse)
    collections = [c.name for c in client.get_collections().collections]
    needs_create = _QDRANT_COLLECTION not in collections

    if not needs_create:
        info = client.get_collection(_QDRANT_COLLECTION)
        existing = info.config.params.vectors
        # Migrate if old unnamed-vector collection
        if existing is not None and not isinstance(existing, dict):
            logger.warning(
                "Migrating '%s' to named vectors (dense+sparse); existing data will be lost",
                _QDRANT_COLLECTION,
            )
            client.delete_collection(_QDRANT_COLLECTION)
            needs_create = True

    if needs_create:
        client.create_collection(
            collection_name=_QDRANT_COLLECTION,
            vectors_config={
                "dense": VectorParams(
                    size=len(dense_embeddings[0]), distance=Distance.COSINE
                ),
            },
            sparse_vectors_config={
                "sparse": SparseVectorParams(index=SparseIndexParams()),
            },
        )

    ingested_at = _now_iso()
    source_date = _source_date()
    points = [
        PointStruct(
            # Deterministic + tenant-namespaced: re-ingesting the same doc updates in
            # place; two tenants (or two docs) can never collide onto one id.
            id=str(uuid.uuid5(_POINT_ID_NAMESPACE, f"{_TENANT_ID}:{source}:{i}")),
            vector={
                "dense": dense_embeddings[i],
                "sparse": _to_sparse_vector(sparse_embeddings[i]),
            },
            payload={
                "text": chunks[i],
                "source": source,
                "chunk_index": i,
                "tenant_id": _TENANT_ID,
                # Freshness (Task 10): ingested_at is always stamped; source_date is the
                # document's own date when the operator supplied one (else null). G07
                # reads these to compute chunk age + apply an optional max-age filter.
                "ingested_at": ingested_at,
                "source_date": source_date,
            },
        )
        for i in range(len(chunks))
    ]
    client.upsert(collection_name=_QDRANT_COLLECTION, points=points)
    logger.info(
        "Upserted %d chunks from '%s' to Qdrant (dense + sparse)",
        len(points),
        source,
    )
    snapshot_collection(client, _QDRANT_COLLECTION)


# ─── Durable collections: a snapshot after every ingest ───────────────────────
# Qdrant on Cloud Run keeps its collections in the container's own filesystem, so a new
# revision or a restart starts it empty. The changed collection's snapshot goes to
# QDRANT_SNAPSHOT_BUCKET, and the service restores the newest snapshot of each collection
# before it serves (infra/qdrant-restore.sh). The name carries the time the snapshot was asked
# for: one asked for later holds every upsert an earlier one does, so the newest is the most
# complete, whichever of two concurrent ingests uploads last.
_SNAPSHOT_TIME = "%Y%m%dT%H%M%S.%fZ"


def snapshot_collection(client, collection: str) -> Optional[str]:
    """Write *collection*'s snapshot to QDRANT_SNAPSHOT_BUCKET as <collection>.<UTC time>.snapshot
    and delete its older snapshots there (never a newer one another ingest wrote). Returns the
    object's name; None when no bucket is set (a Qdrant with storage of its own). Qdrant's own
    copy is deleted whatever happens: it holds the whole collection in the container's memory.
    A failure raises, failing the ingest: the document would not survive a restart."""
    bucket_name = os.getenv("QDRANT_SNAPSHOT_BUCKET", "").strip()
    if not bucket_name:
        return None
    name = f"{collection}.{datetime.now(timezone.utc).strftime(_SNAPSHOT_TIME)}.snapshot"
    snapshot = client.create_snapshot(collection_name=collection, wait=True)
    try:
        from google.cloud import storage
        bucket = storage.Client().bucket(bucket_name)
        with tempfile.TemporaryFile() as copy:
            _download_snapshot(collection, snapshot.name, copy)
            copy.seek(0)
            bucket.blob(name).upload_from_file(copy, content_type="application/octet-stream")
        for blob in bucket.list_blobs(prefix=f"{collection}."):
            if blob.name.endswith(".snapshot") and blob.name < name:
                blob.delete()
    finally:
        client.delete_snapshot(collection_name=collection, snapshot_name=snapshot.name, wait=True)
    logger.info("Snapshot of %s written to gs://%s/%s", collection, bucket_name, name)
    return name


def _download_snapshot(collection: str, snapshot_name: str, out) -> None:
    """Stream a snapshot file from Qdrant's HTTP API into *out*, with the client's credentials:
    the API key, and on GCP an identity token for Cloud Run's IAM."""
    import httpx

    headers = {}
    if os.getenv("QDRANT_API_KEY"):
        headers["api-key"] = os.environ["QDRANT_API_KEY"]
    if _QDRANT_URL.startswith("https://") and os.getenv("QDRANT_LOCAL_NOAUTH") != "1" and _on_gcp():
        headers["Authorization"] = f"Bearer {_gcp_identity_token(_QDRANT_URL)}"
    url = f"{_QDRANT_URL.rstrip('/')}/collections/{collection}/snapshots/{snapshot_name}"
    with httpx.stream("GET", url, headers=headers, timeout=600.0) as response:
        response.raise_for_status()
        for chunk in response.iter_bytes():
            out.write(chunk)


class IngestPiiError(RuntimeError):
    """INGEST_PII_MODE asks for something this job cannot do. Storing the text anyway would
    drop the operator's decision, so the job fails instead."""


def redact_ingest_pii(text: str) -> str:
    """Mask/flag PII (and optional PHI) in a document BEFORE it is chunked, embedded,
    and stored — so the vector store never holds raw personal data and G07 retrieval
    can never inject it into a prompt. Scanning the full text before chunking also
    stops a PII value being split across a chunk boundary and evading the scan.

    Runtime config (Cloud Run Job env):
      * ``INGEST_PII_MODE`` — ``off`` (default; ingestion unchanged) | ``flag`` (detect
        + log, do not mutate) | ``mask`` (replace in place, irreversible — there is no
        response to restore at ingest).
      * ``INGEST_PII_PHI``  — ``true`` also scans the PHI entity set.

    Uses the shared OSS ``guardrails`` engine. If it isn't importable in this container,
    flag mode warns and goes on (it never changes the text), while mask mode raises
    IngestPiiError rather than store unmasked text, as does a mode other than off, flag
    or mask (a mistyped mask must not mean off)."""
    mode = os.getenv("INGEST_PII_MODE", "").strip().lower() or "off"
    if mode not in ("off", "flag", "mask"):
        raise IngestPiiError(f"INGEST_PII_MODE={mode!r} is not off, flag or mask")
    if mode == "off" or not text:
        return text
    try:
        from guardrails.pii import PiiDetector, mask_matches, DEFAULT_ENTITIES, PHI_ENTITIES
    except Exception as exc:
        if mode == "mask":
            raise IngestPiiError("INGEST_PII_MODE=mask but the guardrails engine is not "
                                 "importable in this container") from exc
        logger.warning(
            "INGEST_PII_MODE=flag but the guardrails engine is not importable in this "
            "container — skipping the ingest PII scan",
        )
        return text
    phi = os.getenv("INGEST_PII_PHI", "false").lower() == "true"
    entities = list(DEFAULT_ENTITIES) + (list(PHI_ENTITIES) if phi else [])
    matches = PiiDetector(entities=entities).detect(text)
    if not matches:
        return text
    types_ = ",".join(sorted({m.entity_type for m in matches}))  # types only — never the value
    if mode == "flag":
        logger.warning("Ingest PII (flag mode, not masked): %d span(s) across %s", len(matches), types_)
        return text
    logger.info("Ingest PII masked: %d span(s) across %s", len(matches), types_)
    return mask_matches(text, matches, reversible=False).text


def run() -> None:
    if not _GCS_BUCKET or not _GCS_OBJECT:
        logger.error("GCS_BUCKET and GCS_OBJECT environment variables are required")
        sys.exit(1)

    logger.info("Processing gs://%s/%s", _GCS_BUCKET, _GCS_OBJECT)

    content = download_from_gcs(_GCS_BUCKET, _GCS_OBJECT)
    try:
        text = extract_text(content, _GCS_OBJECT)
    except UnreadableDocumentError as exc:
        logger.error("%s — not ingesting gs://%s/%s", exc, _GCS_BUCKET, _GCS_OBJECT)
        sys.exit(1)
    text = strip_boilerplate(text)

    if len(text) < 50:
        logger.warning("Extracted text too short — skipping")
        return

    # Tables → compact CSV (reduces token cost of table-heavy docs)
    text = table_to_csv(text)

    # Trust & safety: redact PII/PHI BEFORE chunk/embed/store (no-op unless
    # INGEST_PII_MODE is flag/mask) so the vector store never holds raw personal data.
    try:
        text = redact_ingest_pii(text)
    except IngestPiiError as exc:
        logger.error("%s — not ingesting gs://%s/%s", exc, _GCS_BUCKET, _GCS_OBJECT)
        sys.exit(1)

    chunks = chunk_text(text)
    logger.info("Created %d chunks", len(chunks))

    # Cut any chunk over MAX_CHUNK_TOKENS into pieces within it.
    max_chunk_tokens = int(os.getenv("MAX_CHUNK_TOKENS", "4000"))
    chunks = split_oversized_chunks(chunks, max_tokens=max_chunk_tokens)
    logger.info("After the size cut: %d chunks", len(chunks))

    dense_embeddings = embed_chunks_dense(chunks)
    sparse_embeddings = embed_chunks_sparse(chunks)
    source = f"gs://{_GCS_BUCKET}/{_GCS_OBJECT}"
    upsert_to_qdrant(chunks, dense_embeddings, sparse_embeddings, source)
    logger.info("Pipeline complete for %s", source)


if __name__ == "__main__":
    run()
