FROM python:3.11-slim

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    curl \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
# CPU-only torch must precede requirements.txt so sentence-transformers
# resolves to it instead of the full CUDA build (~2.5 GB) from PyPI.
RUN pip install --no-cache-dir torch --index-url https://download.pytorch.org/whl/cpu
RUN pip install --no-cache-dir -r requirements.txt

# Pre-bake the G05 L2 semantic-cache embedding model (bge-small) into the image so
# there is NO runtime download. Essential for stateless GCP Cloud Run (no persistent
# cache between revisions/instances would otherwise re-download ~130MB on every cold
# start) and it removes the first-deploy cold-load locally too. Pinned here for
# reproducible builds. Kept before `COPY . .` so app-code changes don't bust this
# ~130MB layer. Must match groups.G5_cache.l2_embedding_model in config.yaml.
# Pinned to an immutable commit SHA (not a floating tag) so every build — including
# cold CI / Cloud Build and `--no-cache` recreates — bakes byte-identical weights.
# An unpinned ref would silently drift if BAAI re-pushes the repo, changing the L2
# cache's 384-dim vector space and poisoning stored cache_l2 rows. Bump deliberately.
ARG L2_EMBEDDING_MODEL=BAAI/bge-small-en-v1.5
ARG L2_EMBEDDING_REVISION=5c38ec7c405ec4b44b94cc5a9bb96e735b38267a
RUN python -c "from sentence_transformers import SentenceTransformer; SentenceTransformer('${L2_EMBEDDING_MODEL}', revision='${L2_EMBEDDING_REVISION}')"

COPY . .

ENV PORT=8080
ENV LOG_LEVEL=INFO

EXPOSE 8080

CMD uvicorn main:app --host 0.0.0.0 --port ${PORT:-8080} --workers 2
