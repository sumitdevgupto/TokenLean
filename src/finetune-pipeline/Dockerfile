# Fine-tuning Pipeline — Cloud Run Job for domain-specific fine-tuning
FROM python:3.11-slim

WORKDIR /app

# Install dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy pipeline code
COPY pipeline.py .

# Run as non-root
USER 1000

ENTRYPOINT ["python", "pipeline.py"]
