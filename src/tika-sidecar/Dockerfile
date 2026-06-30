# Standard Tika image — no OCR bundle needed (only text-based docs flow through this pipeline).
# The -full tag (Tesseract OCR) added ~1 GB with no benefit for this workload.
FROM apache/tika:2.9.1.0

EXPOSE 9998

# Tool-independent healthcheck (bash /dev/tcp) — wget absent in standard image.
# Same pattern used by qdrant in docker-compose.
HEALTHCHECK --interval=10s --timeout=5s --start-period=30s --retries=5 \
    CMD bash -c 'exec 3<>/dev/tcp/localhost/9998' || exit 1

LABEL maintainer="token-optimisation-proxy" \
      component="tika-sidecar" \
      version="2.9.1"

ENV TIKA_MAX_FILE_SIZE=100000000 \
    TIKA_TIMEOUT_MS=30000
