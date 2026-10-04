# Fine-tuning Pipeline — Cloud Run Job for domain-specific fine-tuning
# Pinned by digest; Dependabot proposes the tag's new digest for review.
FROM python:3.11-slim@sha256:94c50be2dc994b873b55bc123e95e6dbade08095b3dfd790f51c34de3f08cbb7

WORKDIR /app

# Install dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy pipeline code
COPY pipeline.py .

# Run as non-root
USER 1000

ENTRYPOINT ["python", "pipeline.py"]
