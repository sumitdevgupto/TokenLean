# Pinned by digest; Dependabot proposes the tag's new digest for review.
FROM python:3.11-slim@sha256:94c50be2dc994b873b55bc123e95e6dbade08095b3dfd790f51c34de3f08cbb7

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential libmagic1 poppler-utils tesseract-ocr \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY pipeline.py .

# Vendored OSS trust-safety engine, staged into the build context by
# scripts/ci/stage-doc-pipeline-guardrails.sh (an explicit list of the proxy's public
# guardrails files); every build path runs it first. Enables opt-in INGEST_PII_MODE masking
# to actually run in this Cloud Run Job; without it the `from guardrails.pii import ...` in
# pipeline.py silently no-ops (default off, so safe). Building by hand: run that script first.
COPY guardrails/ ./guardrails/

# Runs as an unprivileged user (uid 1000), not root.
RUN useradd --create-home --uid 1000 --user-group app
USER 1000

ENV LOG_LEVEL=INFO

CMD ["python", "pipeline.py"]
