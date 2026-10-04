# Pinned by digest: a rebuild takes the same OS and Python patch level. Dependabot proposes
# the tag's new digest for review (.github/dependabot.yml), never a new tag.
FROM python:3.11-slim@sha256:94c50be2dc994b873b55bc123e95e6dbade08095b3dfd790f51c34de3f08cbb7

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    curl \
    && rm -rf /var/lib/apt/lists/*

# The proxy runs as an unprivileged user (uid 1000), not root. The model cache lives outside
# /root, in a directory that user owns: the bake below runs as it, so the cache is its own.
# It owns /app itself, where a local export (STORAGE_BACKEND=local) writes.
RUN useradd --create-home --uid 1000 --user-group app \
    && mkdir -p /opt/hf-cache && chown app:app /opt/hf-cache /app
ENV HF_HOME=/opt/hf-cache

COPY requirements.txt .
# CPU-only torch must precede requirements.txt so sentence-transformers
# resolves to it instead of the full CUDA build (~2.5 GB) from PyPI. requirements.txt
# leaves it (and its CUDA companions) out, so it is pinned here: at the version
# requirements.in names, which the pins are resolved against.
RUN pip install --no-cache-dir torch==2.14.0 --index-url https://download.pytorch.org/whl/cpu
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
# The revision-pinned download creates ONLY snapshots/<sha> — huggingface_hub writes a
# refs/<name> file for branch/tag revisions, NOT for commit SHAs. Runtime loads the model
# WITHOUT a revision (resolves "main" → needs refs/main), so an offline/egress-restricted
# host cache-MISSES the baked model. Write refs/main → pinned SHA ourselves (the bare SHA,
# no trailing newline — exactly what hf_hub writes for a branch) so the offline lookup
# resolves while the build stays immutably pinned.
USER 1000
RUN python -c "from sentence_transformers import SentenceTransformer; SentenceTransformer('${L2_EMBEDDING_MODEL}', revision='${L2_EMBEDDING_REVISION}')" && \
    REF_DIR="${HF_HOME}/hub/models--$(echo "${L2_EMBEDDING_MODEL}" | sed 's|/|--|g')/refs" && \
    mkdir -p "$REF_DIR" && printf '%s' "${L2_EMBEDDING_REVISION}" > "$REF_DIR/main"

COPY . .

ENV PORT=8080
ENV LOG_LEVEL=INFO
# Prometheus metric objects are constructed at module IMPORT time, which is when
# prometheus_client picks its value class — so this must come from the container
# environment; Python code cannot set it in time. Without it, each of the two
# uvicorn workers keeps a private registry and /metrics answers from whichever one
# the OS routed to (observed live: 69/69/23/69/23/69 on six consecutive scrapes).
ENV PROMETHEUS_MULTIPROC_DIR=/tmp/prometheus-multiproc

EXPOSE 8080

# Purge before start: `docker restart` keeps the container layer, so stale per-PID
# mmap files from the previous run would otherwise be summed into the aggregate.
# `exec` keeps uvicorn as PID 1 so it still receives SIGTERM.
CMD sh -c 'rm -rf "$PROMETHEUS_MULTIPROC_DIR" && mkdir -p "$PROMETHEUS_MULTIPROC_DIR" && exec uvicorn main:app --no-proxy-headers --host 0.0.0.0 --port ${PORT:-8080} --workers 2'
