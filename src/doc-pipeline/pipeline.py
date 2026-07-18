"""
G03 · Document Ingestion Pipeline — Cloud Run Job
Triggered when a document is uploaded to GCS.
Steps: download → extract text (Unstructured) → strip boilerplate
       → chunk (256-512 tokens) → embed dense + sparse (SPLADE/BM25)
       → upsert to Qdrant named vectors.
"""
import logging
import os
import re
import sys
import uuid
from typing import List

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


def download_from_gcs(bucket: str, obj: str) -> bytes:
    from google.cloud import storage
    client = storage.Client()
    blob = client.bucket(bucket).blob(obj)
    return blob.download_as_bytes()


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
                },
            )
            resp.raise_for_status()
            return resp.text
    except Exception as exc:
        logger.debug("Tika extraction failed: %s", exc)
        return ""


def extract_text(content: bytes, filename: str) -> str:
    """Use Unstructured or Tika to extract clean text from any document type."""
    # Try Tika first if enabled
    use_tika = os.getenv("USE_TIKA", "false").lower() == "true"
    if use_tika:
        text = extract_text_with_tika(content, filename)
        if text:
            logger.info("Extracted text using Tika sidecar")
            return text
        logger.warning("Tika failed — falling back to Unstructured")
    
    # Fallback to Unstructured
    try:
        import tempfile
        from unstructured.partition.auto import partition

        with tempfile.NamedTemporaryFile(suffix=os.path.splitext(filename)[1], delete=False) as f:
            f.write(content)
            tmp_path = f.name

        elements = partition(filename=tmp_path)
        os.unlink(tmp_path)
        return "\n\n".join(str(e) for e in elements)
    except Exception as exc:
        logger.error("Text extraction failed: %s", exc)
        # Fallback: decode as UTF-8
        try:
            return content.decode("utf-8", errors="replace")
        except Exception:
            return ""


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


def summarise_large_chunks(
    chunks: List[str], max_tokens: int = 4000
) -> List[str]:
    """
    Summarise any chunk exceeding max_tokens with a cheap model.
    Avoids sending oversized chunks to the vector DB (and later to the LLM).
    """
    import tiktoken
    try:
        enc = tiktoken.get_encoding("cl100k_base")
    except Exception:
        enc = None

    result: List[str] = []
    for chunk in chunks:
        token_count = len(enc.encode(chunk)) if enc else len(chunk) // 4
        if token_count <= max_tokens:
            result.append(chunk)
            continue

        # Summarise with cheap LLM
        try:
            import litellm
            summary_model = os.getenv("SUMMARY_MODEL", "gemini-2.0-flash-lite")
            provider_key = os.getenv("SUMMARY_PROVIDER_KEY", "")

            # Try to resolve from Secret Manager if running on GCP
            if not provider_key:
                try:
                    from google.cloud import secretmanager
                    sm = secretmanager.SecretManagerServiceClient()
                    project = os.getenv("GOOGLE_CLOUD_PROJECT", "")
                    if project:
                        secret_name = f"projects/{project}/secrets/gemini-api-key/versions/latest"
                        resp = sm.access_secret_version(name=secret_name)
                        provider_key = resp.payload.data.decode("utf-8").strip()
                except Exception:
                    pass

            response = litellm.completion(
                model=summary_model,
                messages=[{
                    "role": "user",
                    "content": (
                        "Summarise the following document section in plain prose. "
                        "Preserve all key facts, numbers, and named entities. "
                        f"Be concise (target <{max_tokens // 2} tokens):\n\n{chunk[:12000]}"
                    ),
                }],
                api_key=provider_key or None,
                max_tokens=max_tokens // 2,
            )
            summary = response.choices[0].message.content or chunk
            result.append(summary)
            logger.info("Summarised oversized chunk: %d tokens → summary", token_count)
        except Exception as exc:
            logger.warning("Chunk summarisation failed: %s — keeping original", exc)
            result.append(chunk)

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

    client = QdrantClient(url=_QDRANT_URL)

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

    Uses the shared OSS ``guardrails`` engine. If it isn't importable in this container
    the scan safely no-ops with a one-time warning (default mode is off, so this never
    changes behaviour unless an operator opted in)."""
    mode = os.getenv("INGEST_PII_MODE", "off").lower()
    if mode not in ("flag", "mask") or not text:
        return text
    try:
        from guardrails.pii import PiiDetector, mask_matches, DEFAULT_ENTITIES, PHI_ENTITIES
    except Exception:
        logger.warning(
            "INGEST_PII_MODE=%s but the guardrails engine is not importable in this "
            "container — skipping ingest PII scan", mode,
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
    text = extract_text(content, _GCS_OBJECT)
    text = strip_boilerplate(text)

    if len(text) < 50:
        logger.warning("Extracted text too short — skipping")
        return

    # Tables → compact CSV (reduces token cost of table-heavy docs)
    text = table_to_csv(text)

    # Trust & safety: redact PII/PHI BEFORE chunk/embed/store (no-op unless
    # INGEST_PII_MODE is flag/mask) so the vector store never holds raw personal data.
    text = redact_ingest_pii(text)

    chunks = chunk_text(text)
    logger.info("Created %d chunks", len(chunks))

    # Summarise oversized chunks (>4,000 tokens) with cheap model
    max_chunk_tokens = int(os.getenv("MAX_CHUNK_TOKENS", "4000"))
    chunks = summarise_large_chunks(chunks, max_tokens=max_chunk_tokens)
    logger.info("After summarisation: %d chunks", len(chunks))

    dense_embeddings = embed_chunks_dense(chunks)
    sparse_embeddings = embed_chunks_sparse(chunks)
    source = f"gs://{_GCS_BUCKET}/{_GCS_OBJECT}"
    upsert_to_qdrant(chunks, dense_embeddings, sparse_embeddings, source)
    logger.info("Pipeline complete for %s", source)


if __name__ == "__main__":
    run()
