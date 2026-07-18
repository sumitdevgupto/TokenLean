FROM python:3.11-slim

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential libmagic1 poppler-utils tesseract-ocr \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY pipeline.py .

# Vendored OSS trust-safety engine (staged into the build context by ci/cloudbuild.yaml
# — the 3 public files only, never the commercial ruleset_feed.py). Enables opt-in
# INGEST_PII_MODE masking to actually run in this Cloud Run Job; without it the
# `from guardrails.pii import ...` in pipeline.py silently no-ops (default off, so safe).
COPY guardrails/ ./guardrails/

ENV LOG_LEVEL=INFO

CMD ["python", "pipeline.py"]
