# Pinned by digest; Dependabot proposes the tag's new digest for review.
FROM python:3.11-slim@sha256:94c50be2dc994b873b55bc123e95e6dbade08095b3dfd790f51c34de3f08cbb7

WORKDIR /app

# Install system dependencies
RUN apt-get update && apt-get install -y \
    gcc \
    && rm -rf /var/lib/apt/lists/*

# Copy requirements and install Python dependencies
COPY requirements.txt .
# CPU-only torch: routellm's mf/causal_llm routers pull torch transitively. requirements.txt
# leaves it (and its CUDA companions) out, so it is pinned here: at the version
# requirements.in names, which the pins are resolved against.
RUN pip install --no-cache-dir torch==2.14.0 --index-url https://download.pytorch.org/whl/cpu
RUN pip install --no-cache-dir -r requirements.txt

# Copy application code
COPY app.py .

# Runs as an unprivileged user (uid 1000), not root: what its routers fetch at start lands in
# that user's home.
RUN useradd --create-home --uid 1000 --user-group app
USER 1000

# Expose port
EXPOSE 8080

# Run the application — PORT is injected by Cloud Run at runtime
CMD uvicorn app:app --host 0.0.0.0 --port ${PORT:-8081}
