"""
G03 · Document Ingestion Pipeline — Cloud Run Job
Triggered when a document is uploaded to GCS.
Steps: download → extract text (Unstructured) → strip boilerplate
       → chunk (256-512 tokens) → embed dense + sparse (SPLADE/BM25)
       → upsert to Qdrant named vectors.
"""
import hashlib
import logging
import os
import sys
from typing import List

logger = logging.getLogger(__name__)
logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"), stream=sys.stdout)

_GCS_BUCKET = os.getenv("GCS_BUCKET", "")
_GCS_OBJECT = os.getenv("GCS_OBJECT", "")
_QDRANT_URL = os.getenv("QDRANT_URL", "http://localhost:6333")
_QDRANT_COLLECTION = os.getenv("QDRANT_COLLECTION", "rag_docs")
_CHUNK_SIZE = int(os.getenv("CHUNK_SIZE_TOKENS", "400"))
_CHUNK_OVERLAP = int(os.getenv("CHUNK_OVERLAP_TOKENS", "50"))
_SPARSE_MODEL = os.getenv("SPARSE_EMBEDDING_MODEL", "Qdrant/bm25")


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
            id=int(hashlib.md5(f"{source}:{i}".encode()).hexdigest()[:8], 16),
            vector={
                "dense": dense_embeddings[i],
                "sparse": _to_sparse_vector(sparse_embeddings[i]),
            },
            payload={"text": chunks[i], "source": source, "chunk_index": i},
        )
        for i in range(len(chunks))
    ]
    client.upsert(collection_name=_QDRANT_COLLECTION, points=points)
    logger.info(
        "Upserted %d chunks from '%s' to Qdrant (dense + sparse)",
        len(points),
        source,
    )


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
