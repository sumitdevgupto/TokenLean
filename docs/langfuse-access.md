# Langfuse UI Access Guide

This document explains how to access the self-hosted [Langfuse](https://langfuse.com) observability UI deployed alongside the Token Optimisation Proxy — for both **local Docker** and **GCP Cloud Run** deployments.

Langfuse ships in the OSS stack (G18 observability). It stores per-request traces, savings metadata, and model/latency breakdowns.

---

## Local (Docker Compose)

When you start the stack with `scripts/local/deploy-local.sh` (see [deployment-local.md](deployment-local.md)), Langfuse runs as the `langfuse` container.

### Access the UI

```
http://localhost:3100
```

Health check:

```bash
curl http://localhost:3100/api/public/health
```

### API keys (pre-seeded)

Local Langfuse auto-initialises a project from `.env` on first boot — no manual key generation required:

```bash
# .env
LANGFUSE_HOST=http://langfuse:3000        # internal docker network URL the proxy uses
LANGFUSE_PUBLIC_KEY=pk-lf-local
LANGFUSE_SECRET_KEY=sk-lf-local
```

The compose file passes these to Langfuse as `LANGFUSE_INIT_PROJECT_PUBLIC_KEY` / `LANGFUSE_INIT_PROJECT_SECRET_KEY`, and to the proxy as `LANGFUSE_PUBLIC_KEY` / `LANGFUSE_SECRET_KEY`, so traces flow with zero extra setup.

### First login

1. Open `http://localhost:3100`.
2. Sign up for an account — the **first user becomes the admin**.
3. The project (with the keys above) already exists; traces appear under it once the proxy handles requests.

### Changing the keys

Edit the three values in `.env`, then recreate the affected containers so the new env is picked up:

```bash
docker-compose up -d --force-recreate langfuse proxy
```

> **Note:** the internal `LANGFUSE_HOST` (`http://langfuse:3000`) is the docker-network address the **proxy** uses to send traces. `http://localhost:3100` is the host-mapped port you use in the **browser**.

---

## GCP (Cloud Run)

### Deployment URL

After running `scripts/gcp/gcp-deploy.sh`, the Langfuse service URL is printed in the deployment summary:

```
Langfuse: https://langfuse-svc-<hash>-<region>.a.run.app
```

### Authentication Modes

#### Option A: Public Access (Development / IAP-protected)

Set the environment variable before deploying:

```bash
export LANGFUSE_UI_PUBLIC=1
./scripts/gcp/gcp-deploy.sh
```

This deploys Langfuse with `--allow-unauthenticated`. In production, place this behind [Cloud IAP](https://cloud.google.com/iap) or a corporate reverse proxy.

#### Option B: Private Access (Default, Recommended)

By default, Langfuse is deployed with `--no-allow-unauthenticated`. Access it via a local tunnel:

```bash
gcloud run services proxy langfuse-svc --region=$REGION --project=$PROJECT_ID
```

Then open `http://localhost:8080` in your browser.

### First Login & Project Setup

1. Open the Langfuse URL in your browser.
2. Sign up for an account (first user becomes admin).
3. Create a new project (e.g. `token-optimisation`).
4. Navigate to **Settings > API Keys**.
5. Generate a **Public** and **Secret** key pair.
6. Store them in Secret Manager (the deploy script does this automatically on first run):
   ```bash
   echo -n "pk-lf-..." | gcloud secrets versions add langfuse-public-key --data-file=-
   echo -n "sk-lf-..." | gcloud secrets versions add langfuse-secret-key --data-file=-
   ```
7. Re-deploy the proxy so it picks up the keys:
   ```bash
   ./scripts/gcp/gcp-deploy.sh --skip-infra
   ```

### Required IAM Roles

| Role | Purpose |
|---|---|
| `roles/run.invoker` | For service accounts (proxy, Grafana) calling Langfuse internally |
| `roles/cloudsql.client` | For Langfuse service to connect to Cloud SQL |

For human operators who need UI access:

- If using **public mode**, no additional IAM role is needed (authentication is handled by Langfuse's built-in login).
- If using **private mode**, ensure the user has `roles/run.viewer` on the Cloud Run service so `gcloud run services proxy` succeeds.

---

## Troubleshooting

| Symptom | Environment | Cause | Fix |
|---|---|---|---|
| Cannot reach `http://localhost:3100` | Local | Langfuse container not up | `docker-compose ps`; check `docker-compose logs langfuse` |
| Traces not appearing | Local | Keys mismatch between proxy and Langfuse | Ensure the same `LANGFUSE_PUBLIC_KEY`/`LANGFUSE_SECRET_KEY` in `.env`; `--force-recreate` both containers |
| `Langfuse shows database error` | Local | Postgres not healthy yet | Wait for postgres: `docker-compose logs postgres` |
| `403 Forbidden` on Langfuse URL | GCP | Service is private | Use `gcloud run services proxy` or set `LANGFUSE_UI_PUBLIC=1` |
| `NEXTAUTH_URL mismatch` | GCP | Proxy URL changed after first deploy | Re-run `gcp-deploy.sh --skip-infra` to patch the env var |
| Traces not appearing | GCP | API keys missing or incorrect | Verify keys in Secret Manager and re-deploy proxy |
| Database connection errors | GCP | Cloud SQL connection misconfigured | Check `DB_CONNECTION` and `LANGFUSE_DB_PASSWORD` secrets |
