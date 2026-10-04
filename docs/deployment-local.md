# Local Deployment Guide

Complete guide for running the TokenLean — Token Optimisation Framework locally via Docker Compose. Zero GCP cost.

---

## Overview

The local deployment runs the entire optimisation stack (G0–G28, G27 reserved — 27 implemented) in Docker containers on your machine. All services communicate via a Docker bridge network.

| Component | Docker Image | Port | Purpose |
|-----------|-------------|------|---------|
| **Proxy** | Built from `src/proxy/Dockerfile` | 4000 | Main LiteLLM proxy + middleware |
| **G1 Compression** | Built from `src/llmlingua-sidecar/Dockerfile` | 8080 | LLMLingua-2 sidecar |
| **G3 Doc Pipeline** | Built from `src/tika-sidecar/Dockerfile` | 9998 | Apache Tika extraction |
| **G4 Bypass** | `pgvector/pgvector:pg15` | 5432 | PostgreSQL cache |
| **G5 Cache** | `redis:7.2-alpine` | 6379 | Redis exact-match cache |
| **G6 Routing** | Built from `src/routellm-sidecar/Dockerfile` | 8081 | RouteLLM cascade |
| **G7 Retrieval** | `qdrant/qdrant:v1.9.0` | 6333 | Vector search |
| **G10 Memory** | Redis + Qdrant | - | Session summaries + agent skills |
| **G18 Observability** | `langfuse/langfuse:2` | 3100 | Tracing UI |
| **G18 Dashboards** | `grafana/grafana-oss:10.4.0` | 3000 | Grafana dashboards (optional profile) |
| **Config** | Local volume mount | - | `config/config.yaml` |

---

## Prerequisites

1. **Docker** >= 24.0 installed and running
2. **Docker Compose** >= 2.20 installed
3. **Git** (to clone the repo)
4. **OpenAI API key** (optional, required for G06 RouteLLM routers)

### Windows Users

Use **Git Bash** or **WSL**. The scripts are written in Bash. PowerShell is not supported for deployment scripts.

```powershell
# Git Bash (recommended)
bash ./scripts/local/deploy-local.sh --seed

# WSL
wsl bash ./scripts/local/deploy-local.sh --seed
```

### Linux Users

The proxy and its sidecars run as an unprivileged user, uid 1000, not root. The proxy reads
`./config` through a bind mount, so on a Linux host whose own user is not uid 1000, make
`config/` and its files readable by others (the default `644` is). Docker Desktop on Windows
and macOS needs nothing.

---

## Environment Setup

```bash
# 1. Copy the local environment template
cp .env.template .env

# 2. Edit with your values
# .env
COMPOSE_PROJECT_NAME=token-opt
DB_PASSWORD=devpassword
OPENAI_API_KEY=sk-...  # Required for RouteLLM
```

All local scripts automatically source `.env` at the repo root if present.

Redis, Qdrant and the Langfuse and Grafana admins each require a credential of their own:
`REDIS_PASSWORD`, `QDRANT_API_KEY`, `LANGFUSE_INIT_USER_PASSWORD` and `GRAFANA_PASSWORD`.
`deploy-local.sh` and `start-local.sh` generate any that `.env` leaves empty, as random values
written into `.env` (never printed), and `docker compose` refuses to start without them. Tools
on this machine read them from `.env`: `redis-cli` inside the Redis container logs in by itself,
and Qdrant's API wants the `api-key` header. Langfuse and Grafana read their admin passwords only
when they first create the admin; an existing stack keeps the password it already has.

The proxy takes no cross-origin browser calls unless `.env` lists the origin in `CORS_ORIGINS`
(or sets `CORS_ALLOW_ALL=true`); the portal does not need it.

---

## Quick Deploy

```bash
# Full deploy with seeding — ~5 minutes
./scripts/local/deploy-local.sh --seed

# Deploy without seeding (if Qdrant already has data)
./scripts/local/deploy-local.sh
```

## Step-by-Step Deployment

### Step 1: Verify Docker

```bash
docker info
docker-compose version
```

### Step 2: Start the Stack

```bash
source .env
./scripts/local/deploy-local.sh --seed
```

This will:
1. Build all images locally (proxy, llmlingua, routellm, tika)
2. Start infrastructure containers (redis, postgres, qdrant)
3. Start application containers (proxy, sidecars, langfuse)
4. Wait for health checks
5. Seed Qdrant with rag_docs

### Step 3: Verify Services

```bash
# Check all containers
docker-compose ps

# Check logs
docker-compose logs -f proxy

# Health check endpoints
curl http://localhost:4000/health   # "degraded" + "not_wired" until the proxy's database is wired
curl http://localhost:8080/health
curl http://localhost:8081/health
curl http://localhost:6333/healthz
curl http://localhost:9998/tika
curl http://localhost:3100/api/public/health
```

### Step 4: Test the Proxy

```bash
curl -X POST http://localhost:4000/v1/chat/completions \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer test-key" \
  -d '{
    "model": "gpt-4o-mini",
    "messages": [{"role": "user", "content": "Hello"}]
  }'
```

### Optional: Prompt quality eval

To run the Promptfoo quality eval against the running local proxy:
```bash
PROXY_URL=http://localhost:4000 bash ci/promptfoo-eval.sh
```
See **Build-Time Quality Gates & Optional Evals** in [DEPLOYMENT.md](../DEPLOYMENT.md) for prerequisites (Node `>= 22.22`), key resolution, and the GCP variant.

---

## Lifecycle Management

### Start (After Initial Deploy)

```bash
./scripts/local/start-local.sh --seed
```

### Stop (Zero Cost)

```bash
./scripts/local/stop-local.sh

# Stop with GCS backup (optional): needs CONFIG_GCS_BUCKET in .env (or
# scripts/local/docker-backup.sh --bucket NAME), a bucket in your own GCP project
./scripts/local/stop-local.sh --backup
```

### Restart

```bash
./scripts/local/stop-local.sh
./scripts/local/start-local.sh
```

### Complete Reset

```bash
docker-compose down -v  # Removes volumes (deletes all data)
./scripts/local/deploy-local.sh --seed
```

---

## Service URLs

| Service | Local URL |
|---------|-----------|
| Proxy API | http://localhost:4000 |
| LLMLingua | http://localhost:8080 |
| RouteLLM | http://localhost:8081 |
| Qdrant | http://localhost:6333 |
| Tika | http://localhost:9998 |
| Langfuse | http://localhost:3100 |
| Grafana | http://localhost:3000 (with `--profile observability`) |
| PostgreSQL | localhost:5432 |
| Redis | localhost:6379 |

---

## Optional: Observability Profile

Start Grafana alongside the core stack:

```bash
docker-compose --profile observability up -d
```

Access Grafana at http://localhost:3000 (default login: admin / the password from `.env`)

---

## Troubleshooting

| Issue | Solution |
|-------|----------|
| `Docker is not running` | Start Docker Desktop or the Docker daemon |
| `port is already allocated` | Stop conflicting services or change ports in `.env` |
| `proxy unhealthy` | Check logs: `docker-compose logs proxy` |
| `RouteLLM routing fails` | Set `OPENAI_API_KEY` in `.env` |
| `Langfuse shows database error` | Wait for postgres to be healthy: `docker-compose logs postgres` |
| `Qdrant empty after restart` | Re-seed: `./scripts/seed-data.sh --qdrant-url http://localhost:6333` |
| Windows line-ending errors | Run: `git config core.autocrlf false` then re-clone |

---

## Resource Requirements

| Service | Memory | Notes |
|---------|--------|-------|
| proxy | 4G | Can reduce to 2G for light workloads |
| llmlingua | 2G | Required for LLMLingua-2 model |
| routellm | 2G | RouteLLM embedding models |
| postgres | 512M | pgvector enabled |
| qdrant | 512M | Vector search |
| redis | 256M | Cache |
| langfuse | 512M | Tracing UI |
| tika | 1G | Document extraction |
| grafana | 256M | Optional dashboards |

**Minimum recommended:** 8GB RAM available for Docker.

---

## Troubleshooting

### Grafana PostgreSQL Datasource — SSL Error

**Symptom:** Grafana LangfuseDB panels show "pq: SSL is not enabled on the server" or connection refused errors.

**Fix:** The provisioned datasource at `dashboard/provisioning/datasources/langfuse-postgres.yaml` must include `sslmode: disable` in `jsonData`. This is already set in the committed config:

```yaml
jsonData:
  sslmode: disable
```

Local Docker PostgreSQL does not use SSL. If you see this error, verify your Grafana container is mounting the correct provisioning directory and restart with `--recreate`.

---

## Migrating to GCP

When ready to move to GCP:

1. Run `./scripts/local/stop-local.sh --backup` to back up Redis data to GCS
2. Follow [deployment-gcp.md](deployment-gcp.md) for GCP deploy
3. Use `./scripts/seed-data.sh --gcp-project YOUR_PROJECT_ID` to seed Qdrant in GCP
