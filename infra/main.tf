terraform {
  required_version = ">= 1.8"
  required_providers {
    google = {
      source  = "hashicorp/google"
      version = "~> 5.0"
    }
    random = {
      source  = "hashicorp/random"
      version = "~> 3.0"
    }
  }

  # Remote state (GCS). Bucket + prefix are supplied at init time via
  # -backend-config (see scripts/gcp/gcp-deploy.sh), keeping this file
  # generic/portable. Locally:
  #   terraform init -migrate-state \
  #     -backend-config="bucket=${PROJECT_ID}-tf-state" \
  #     -backend-config="prefix=token-opt"
  # The bucket is versioned so a corrupted/deleted state can be recovered.
  backend "gcs" {}
}

provider "google" {
  project = var.project_id
  region  = var.region
}

# ─── Enable required APIs ────────────────────────────────────────────────────
resource "google_project_service" "apis" {
  for_each = toset([
    "run.googleapis.com",
    "sqladmin.googleapis.com",
    "storage.googleapis.com",
    "secretmanager.googleapis.com",
    "cloudtasks.googleapis.com",
    "artifactregistry.googleapis.com",
    # compute: GCE docker-Redis VM (redis_backend=docker) + Cloud Run Direct VPC egress
    "compute.googleapis.com",
    # monitoring: Cloud Monitoring alert policies (managed observability)
    "monitoring.googleapis.com",
    # servicenetworking: Private Service Access for private Cloud SQL (item 8, opt-in)
    "servicenetworking.googleapis.com",
    # cloudkms: envelope-encrypt the BYOK master key (item 6, opt-in)
    "cloudkms.googleapis.com",
  ])
  service            = each.value
  disable_on_destroy = false
}


# ─── Artifact Registry ───────────────────────────────────────────────────────
resource "google_artifact_registry_repository" "repo" {
  location      = var.region
  repository_id = var.artifact_registry_repo
  format        = "DOCKER"
  description   = "Token optimisation framework Docker images"
  depends_on    = [google_project_service.apis]
}

# ─── GCS Bucket (config + Redis export) ──────────────────────────────────────
resource "random_id" "bucket_suffix" {
  byte_length = 4
}

locals {
  bucket_name = var.config_bucket_name != "" ? var.config_bucket_name : "token-opt-config-${random_id.bucket_suffix.hex}"
}

resource "google_storage_bucket" "config" {
  name                        = local.bucket_name
  location                    = var.region
  force_destroy               = false
  uniform_bucket_level_access = true

  versioning { enabled = true }

  lifecycle_rule {
    action { type = "Delete" }
    condition { age = 90 }
  }
}

# ─── Cloud SQL (PostgreSQL 15 + pgvector) ────────────────────────────────────
resource "random_password" "db_password" {
  length  = 32
  special = true
}

# Item 8 (opt-in): Private Service Access so Cloud SQL can sit on a private IP.
# Only created when var.private_cloud_sql = true, so the default public-IP deploy is
# untouched. Uses the project's default VPC network.
data "google_compute_network" "default" {
  count = var.private_cloud_sql ? 1 : 0
  name  = "default"
}

resource "google_compute_global_address" "sql_psa_range" {
  count         = var.private_cloud_sql ? 1 : 0
  name          = "token-opt-sql-psa"
  purpose       = "VPC_PEERING"
  address_type  = "INTERNAL"
  prefix_length = 16
  network       = data.google_compute_network.default[0].id
}

resource "google_service_networking_connection" "sql_psa" {
  count                   = var.private_cloud_sql ? 1 : 0
  network                 = data.google_compute_network.default[0].id
  service                 = "servicenetworking.googleapis.com"
  reserved_peering_ranges = [google_compute_global_address.sql_psa_range[0].name]
}

resource "google_sql_database_instance" "main" {
  name             = "token-opt-pg"
  database_version = "POSTGRES_15"
  region           = var.region

  settings {
    tier              = var.db_tier
    availability_type = "ZONAL"
    disk_size         = 20
    disk_autoresize   = true

    # Item 8: when private_cloud_sql, drop the public IPv4, peer into the default VPC
    # and require SSL. Otherwise keep the current public-IPv4 posture byte-identical.
    ip_configuration {
      ipv4_enabled    = var.private_cloud_sql ? false : true
      private_network = var.private_cloud_sql ? data.google_compute_network.default[0].id : null
      ssl_mode        = var.private_cloud_sql ? "ENCRYPTED_ONLY" : null
    }

    database_flags {
      name  = "max_connections"
      value = "100"
    }
  }

  deletion_protection = true
  depends_on = [
    google_project_service.apis,
    google_service_networking_connection.sql_psa,
  ]
}

resource "google_sql_database" "langfuse" {
  name     = "langfuse"
  instance = google_sql_database_instance.main.name
}

resource "google_sql_database" "proxy" {
  name     = "token_opt"
  instance = google_sql_database_instance.main.name
}

resource "google_sql_user" "app_user" {
  name     = "token_opt_app"
  instance = google_sql_database_instance.main.name
  password = random_password.db_password.result
}


# ─── Secret Manager ──────────────────────────────────────────────────────────
resource "google_secret_manager_secret" "db_password" {
  secret_id = "token-opt-db-password"
  replication {
    auto {}
  }
  depends_on = [google_project_service.apis]
}

resource "google_secret_manager_secret_version" "db_password" {
  secret      = google_secret_manager_secret.db_password.id
  secret_data = random_password.db_password.result
}

resource "google_secret_manager_secret" "proxy_api_keys" {
  secret_id = "token-proxy-api-keys"
  replication {
    auto {}
  }
  depends_on = [google_project_service.apis]
}

resource "google_secret_manager_secret_version" "proxy_api_keys_init" {
  secret      = google_secret_manager_secret.proxy_api_keys.id
  secret_data = "{}"
}

# LLM provider key secrets (populated by gcp-deploy.sh)
resource "google_secret_manager_secret" "llm_key_openai" {
  secret_id = "llm-key-openai"
  replication {
    auto {}
  }
  depends_on = [google_project_service.apis]
}

resource "google_secret_manager_secret" "llm_key_anthropic" {
  secret_id = "llm-key-anthropic"
  replication {
    auto {}
  }
  depends_on = [google_project_service.apis]
}

resource "google_secret_manager_secret" "llm_key_google" {
  secret_id = "llm-key-google"
  replication {
    auto {}
  }
  depends_on = [google_project_service.apis]
}

resource "google_secret_manager_secret" "llm_key_mistral" {
  secret_id = "llm-key-mistral"
  replication {
    auto {}
  }
  depends_on = [google_project_service.apis]
}

# Additional first-class provider key secrets (10-provider support; populated by
# gcp-deploy.sh). The proxy SA reads these via the Secret Manager client at runtime — the
# project-level secretmanager.secretAccessor binding (proxy_secret_accessor) already covers
# them, so no per-secret IAM is required.
resource "google_secret_manager_secret" "llm_key_extra" {
  for_each  = toset(["gemini", "cohere", "deepseek", "xai", "groq", "azure", "openrouter", "opencode"])
  secret_id = "llm-key-${each.key}"
  replication {
    auto {}
  }
  depends_on = [google_project_service.apis]
}

# AWS Bedrock uses SigV4 credentials (no single bearer key). litellm reads them from the
# environment, so the proxy deploy mounts these as env vars (see gcp-deploy.sh proxy
# --set-secrets / --set-env-vars). Optional — only populate if you route to Bedrock.
resource "google_secret_manager_secret" "aws_access_key_id" {
  secret_id = "aws-access-key-id"
  replication {
    auto {}
  }
  depends_on = [google_project_service.apis]
}

resource "google_secret_manager_secret" "aws_secret_access_key" {
  secret_id = "aws-secret-access-key"
  replication {
    auto {}
  }
  depends_on = [google_project_service.apis]
}

# RouteLLM OpenAI API key (required for embeddings by mf/sw_ranking routers)
resource "google_secret_manager_secret" "routellm_openai_key" {
  secret_id = "routellm-openai-key"
  replication {
    auto {}
  }
  depends_on = [google_project_service.apis]
}

# Langfuse API keys (populated by gcp-deploy.sh after first deploy)
resource "google_secret_manager_secret" "langfuse_public_key" {
  secret_id = "langfuse-public-key"
  replication {
    auto {}
  }
  depends_on = [google_project_service.apis]
}

resource "google_secret_manager_secret" "langfuse_secret_key" {
  secret_id = "langfuse-secret-key"
  replication {
    auto {}
  }
  depends_on = [google_project_service.apis]
}

# Grafana admin password
resource "google_secret_manager_secret" "grafana_admin_password" {
  secret_id = "grafana-admin-password"
  replication {
    auto {}
  }
  depends_on = [google_project_service.apis]
}

# Langfuse NEXTAUTH_SECRET
resource "google_secret_manager_secret" "langfuse_nextauth_secret" {
  secret_id = "langfuse-nextauth-secret"
  replication {
    auto {}
  }
  depends_on = [google_project_service.apis]
}

# NOTE: OpenMeter was removed from the stack (2026-07-02) — it needs a full
# ClickHouse + Kafka backend; billing is computed from the usage_events table
# (below) without it.

# ─── Billing: usage_events Cloud SQL table migration ─────────────────────────
# Applied once against the Cloud SQL Postgres instance via a null_resource
# provisioner so no state is kept in Cloud SQL itself.
resource "null_resource" "billing_schema_migration" {
  # PRIVATE-IP path: the off-VPC deploy host (laptop/WSL) cannot reach the private
  # 10.x instance even via cloud-sql-proxy --private-ip, so these local-exec
  # provisioners are DISABLED when private_cloud_sql=true and the migrations are
  # instead applied from an in-VPC Cloud Run Job (scripts/gcp/run-migrations-job.sh,
  # invoked by gcp-deploy.sh after terraform apply). On the public-IP OSS default
  # the local-exec path is byte-identical to before.
  count = var.private_cloud_sql ? 0 : 1
  triggers = {
    schema_version = "2" # v2: +protocol column (#4 multi-protocol ingress)
  }

  # PGPASSWORD is required so psql does not prompt interactively — without it
  # terraform apply hangs indefinitely. We fetch the password from Secret
  # Manager at apply time.
  #
  # The Cloud SQL instance is private-IP only, so `gcloud sql connect` (which
  # needs a public IP to allowlist) cannot reach it. We instead tunnel via the
  # Cloud SQL Auth Proxy (cloud-sql-proxy) on 127.0.0.1:6543 and run the
  # migration through psql. Same pattern as
  # scripts/commercial/billing/run-invoicing-gcp.sh.
  provisioner "local-exec" {
    # This command is bash (heredocs, $(...), PGPASSWORD). On Windows, Terraform
    # otherwise runs local-exec via cmd.exe, which cannot parse it. Pin bash.
    interpreter = ["bash", "-c"]
    command     = <<-SHELL
      DB_PASS=$(gcloud secrets versions access latest \
        --secret="${google_secret_manager_secret.db_password.secret_id}" \
        --project="${var.project_id}" 2>/dev/null)
      cloud-sql-proxy --port 6543 "${google_sql_database_instance.main.connection_name}" \
        >/tmp/tf_sqlproxy_billing.log 2>&1 &
      PROXY_PID=$!
      trap 'kill "$PROXY_PID" 2>/dev/null || true' EXIT
      # Wait for the tunnel to accept connections (max ~15s).
      for _ in $(seq 1 30); do
        if (exec 3<>/dev/tcp/127.0.0.1/6543) 2>/dev/null; then exec 3>&- 3<&-; break; fi
        sleep 0.5
      done
      (exec 3<>/dev/tcp/127.0.0.1/6543) 2>/dev/null && exec 3>&- 3<&- || \
        { cat /tmp/tf_sqlproxy_billing.log; echo "cloud-sql-proxy did not open port 6543" >&2; exit 1; }
      PGPASSWORD="$DB_PASS" psql -h 127.0.0.1 -p 6543 \
        -U token_opt_app -d token_opt -v ON_ERROR_STOP=1 <<'EOSQL'
CREATE TABLE IF NOT EXISTS usage_events (
  id             BIGSERIAL PRIMARY KEY,
  tenant_id      TEXT        NOT NULL,
  request_id     TEXT        NOT NULL UNIQUE,
  timestamp      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  baseline_tokens   INT     NOT NULL DEFAULT 0,
  optimised_tokens  INT     NOT NULL DEFAULT 0,
  tokens_saved      INT     NOT NULL DEFAULT 0,
  cost_saved_usd    NUMERIC(12,8) NOT NULL DEFAULT 0,
  groups_applied    TEXT[]  NOT NULL DEFAULT '{}',
  pricing_tier      TEXT    NOT NULL DEFAULT 'free'
);
CREATE INDEX IF NOT EXISTS idx_usage_events_tenant_id
  ON usage_events (tenant_id);
CREATE INDEX IF NOT EXISTS idx_usage_events_timestamp
  ON usage_events (timestamp DESC);
-- Requests Explorer filter columns (the app's startup DDL also self-heals these
-- via ALTER ... IF NOT EXISTS; kept here so a fresh GCP provision matches).
ALTER TABLE usage_events ADD COLUMN IF NOT EXISTS user_id TEXT NOT NULL DEFAULT '';
ALTER TABLE usage_events ADD COLUMN IF NOT EXISTS cache_hit BOOLEAN NOT NULL DEFAULT false;
ALTER TABLE usage_events ADD COLUMN IF NOT EXISTS cache_level TEXT NOT NULL DEFAULT '';
ALTER TABLE usage_events ADD COLUMN IF NOT EXISTS complexity_tier TEXT NOT NULL DEFAULT '';
ALTER TABLE usage_events ADD COLUMN IF NOT EXISTS bypassed BOOLEAN NOT NULL DEFAULT false;
-- #4 multi-protocol ingress: which client protocol served this request (never billed).
-- Default must equal protocols.base.DEFAULT_PROTOCOL_NAME (the app-side source of truth).
ALTER TABLE usage_events ADD COLUMN IF NOT EXISTS protocol TEXT NOT NULL DEFAULT 'openai';
EOSQL
    SHELL
  }

  depends_on = [
    google_sql_database.proxy,
    google_sql_user.app_user,
    google_secret_manager_secret_version.db_password,
  ]
}

# ─── E2: tenant_configs Cloud SQL table migration ────────────────────────────
resource "null_resource" "tenant_configs_schema_migration" {
  # Disabled on the private-IP path (applied via the in-VPC migration job instead).
  count = var.private_cloud_sql ? 0 : 1
  triggers = {
    schema_version = "1"
  }

  # Private-IP instance: tunnel via cloud-sql-proxy (127.0.0.1:6544) + psql
  # instead of `gcloud sql connect` (which needs a public IP).
  provisioner "local-exec" {
    interpreter = ["bash", "-c"] # bash heredoc/$(...) — not cmd.exe (Windows local-exec default)
    command     = <<-SHELL
      DB_PASS=$(gcloud secrets versions access latest \
        --secret="${google_secret_manager_secret.db_password.secret_id}" \
        --project="${var.project_id}" 2>/dev/null)
      cloud-sql-proxy --port 6544 "${google_sql_database_instance.main.connection_name}" \
        >/tmp/tf_sqlproxy_tenant_configs.log 2>&1 &
      PROXY_PID=$!
      trap 'kill "$PROXY_PID" 2>/dev/null || true' EXIT
      # Wait for the tunnel to accept connections (max ~15s).
      for _ in $(seq 1 30); do
        if (exec 3<>/dev/tcp/127.0.0.1/6544) 2>/dev/null; then exec 3>&- 3<&-; break; fi
        sleep 0.5
      done
      (exec 3<>/dev/tcp/127.0.0.1/6544) 2>/dev/null && exec 3>&- 3<&- || \
        { cat /tmp/tf_sqlproxy_tenant_configs.log; echo "cloud-sql-proxy did not open port 6544" >&2; exit 1; }
      PGPASSWORD="$DB_PASS" psql -h 127.0.0.1 -p 6544 \
        -U token_opt_app -d token_opt -v ON_ERROR_STOP=1 <<'EOSQL'
CREATE TABLE IF NOT EXISTS tenant_configs (
  tenant_id        TEXT        PRIMARY KEY,
  config_overrides JSONB       NOT NULL DEFAULT '{}',
  updated_at       TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
EOSQL
    SHELL
  }

  depends_on = [
    google_sql_database.proxy,
    google_sql_user.app_user,
    google_secret_manager_secret_version.db_password,
    null_resource.billing_schema_migration,
  ]
}

# ─── F2: audit_events Cloud SQL table migration + INSERT-only role ───────────
resource "null_resource" "audit_events_schema_migration" {
  # Disabled on the private-IP path (applied via the in-VPC migration job instead).
  count = var.private_cloud_sql ? 0 : 1
  triggers = {
    # v2 (WS8): + details JSONB for config-change audit events. The app also
    # ensures this column at startup (audit.log.ensure_audit_schema) - this bump
    # keeps GCP databases drift-free even before the first commercial boot.
    schema_version = "2"
  }

  # Private-IP instance: tunnel via cloud-sql-proxy (127.0.0.1:6545) + psql
  # instead of `gcloud sql connect` (which needs a public IP).
  provisioner "local-exec" {
    interpreter = ["bash", "-c"] # bash heredoc/$(...) — not cmd.exe (Windows local-exec default)
    command     = <<-SHELL
      DB_PASS=$(gcloud secrets versions access latest \
        --secret="${google_secret_manager_secret.db_password.secret_id}" \
        --project="${var.project_id}" 2>/dev/null)
      cloud-sql-proxy --port 6545 "${google_sql_database_instance.main.connection_name}" \
        >/tmp/tf_sqlproxy_audit_events.log 2>&1 &
      PROXY_PID=$!
      trap 'kill "$PROXY_PID" 2>/dev/null || true' EXIT
      # Wait for the tunnel to accept connections (max ~15s).
      for _ in $(seq 1 30); do
        if (exec 3<>/dev/tcp/127.0.0.1/6545) 2>/dev/null; then exec 3>&- 3<&-; break; fi
        sleep 0.5
      done
      (exec 3<>/dev/tcp/127.0.0.1/6545) 2>/dev/null && exec 3>&- 3<&- || \
        { cat /tmp/tf_sqlproxy_audit_events.log; echo "cloud-sql-proxy did not open port 6545" >&2; exit 1; }
      PGPASSWORD="$DB_PASS" psql -h 127.0.0.1 -p 6545 \
        -U token_opt_app -d token_opt -v ON_ERROR_STOP=1 <<'EOSQL'
CREATE TABLE IF NOT EXISTS audit_events (
  id             BIGSERIAL    PRIMARY KEY,
  tenant_id      TEXT         NOT NULL,
  request_id     TEXT         NOT NULL,
  timestamp      TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
  action         TEXT         NOT NULL DEFAULT 'proxy_request',
  user_id        TEXT,
  groups_applied TEXT[]       NOT NULL DEFAULT '{}',
  tokens_saved   INT          NOT NULL DEFAULT 0,
  otel_trace_id  TEXT
);
CREATE INDEX IF NOT EXISTS idx_audit_events_tenant_id
  ON audit_events (tenant_id);
CREATE INDEX IF NOT EXISTS idx_audit_events_timestamp
  ON audit_events (timestamp DESC);
ALTER TABLE audit_events ADD COLUMN IF NOT EXISTS details JSONB;
DO $$ BEGIN
  IF NOT EXISTS (
    SELECT FROM pg_roles WHERE rolname = 'proxy_audit_role'
  ) THEN
    CREATE ROLE proxy_audit_role;
  END IF;
END $$;
REVOKE ALL ON audit_events FROM proxy_audit_role;
GRANT INSERT ON audit_events TO proxy_audit_role;
EOSQL
    SHELL
  }

  depends_on = [
    google_sql_database.proxy,
    google_sql_user.app_user,
    google_secret_manager_secret_version.db_password,
    null_resource.billing_schema_migration,
  ]
}

# ─── I2: Row-Level Security policies on the tenant tables (defense-in-depth) ──
resource "null_resource" "rls_policies_migration" {
  # Disabled on the private-IP path (applied via the in-VPC migration job instead).
  count = var.private_cloud_sql ? 0 : 1
  triggers = {
    schema_version = "1"
    sql_sha        = filesha256("${path.module}/migrations/rls_policies.sql")
  }

  # Private-IP instance: tunnel via cloud-sql-proxy (127.0.0.1:6546) + psql
  # instead of `gcloud sql connect` (which needs a public IP).
  provisioner "local-exec" {
    interpreter = ["bash", "-c"] # bash heredoc/$(...) — not cmd.exe (Windows local-exec default)
    command     = <<-SHELL
      DB_PASS=$(gcloud secrets versions access latest \
        --secret="${google_secret_manager_secret.db_password.secret_id}" \
        --project="${var.project_id}" 2>/dev/null)
      cloud-sql-proxy --port 6546 "${google_sql_database_instance.main.connection_name}" \
        >/tmp/tf_sqlproxy_rls.log 2>&1 &
      PROXY_PID=$!
      trap 'kill "$PROXY_PID" 2>/dev/null || true' EXIT
      # Wait for the tunnel to accept connections (max ~15s).
      for _ in $(seq 1 30); do
        if (exec 3<>/dev/tcp/127.0.0.1/6546) 2>/dev/null; then exec 3>&- 3<&-; break; fi
        sleep 0.5
      done
      (exec 3<>/dev/tcp/127.0.0.1/6546) 2>/dev/null && exec 3>&- 3<&- || \
        { cat /tmp/tf_sqlproxy_rls.log; echo "cloud-sql-proxy did not open port 6546" >&2; exit 1; }
      PGPASSWORD="$DB_PASS" psql -h 127.0.0.1 -p 6546 \
        -U token_opt_app -d token_opt -v ON_ERROR_STOP=1 \
        -f "${path.module}/migrations/rls_policies.sql"
    SHELL
  }

  depends_on = [
    null_resource.audit_events_schema_migration,
  ]
}

# NOTE: SLA alert rules (sla_alert_rules → alert_rules.yaml) moved to
# infra/commercial.tf (open-core split, item 11/35) — SLA alerting is PAID.

# ─── Service Accounts ────────────────────────────────────────────────────────
resource "google_service_account" "proxy_sa" {
  account_id   = "token-opt-proxy-sa"
  display_name = "Token Optimisation Proxy"
}

# Item 7 (opt-in): least-privilege secret access. Default keeps the project-wide
# secretmanager.secretAccessor grant; when var.least_privilege_secret_iam = true the
# broad grant is dropped and the proxy SA is bound to ONLY the secrets it reads.
#
# NOTE: three commercial secrets are created by scripts/commercial/deploy-commercial-gcp.sh
# (gcloud secrets create), NOT by Terraform, so they cannot be referenced here by resource
# attribute: `tenant-key-encryption-key` (the BYOK master key), `database-url`, and
# `sendgrid-api-key`. That script binds the proxy SA to each at creation time via
# `grant_secret_access` (gated on least_privilege_secret_iam). If you add another
# script-created secret the proxy must read, bind it there too — this list is Terraform-owned
# secrets only.
locals {
  proxy_secret_ids = var.least_privilege_secret_iam ? concat([
    google_secret_manager_secret.db_password.secret_id,
    google_secret_manager_secret.proxy_api_keys.secret_id,
    google_secret_manager_secret.llm_key_openai.secret_id,
    google_secret_manager_secret.llm_key_anthropic.secret_id,
    google_secret_manager_secret.llm_key_google.secret_id,
    google_secret_manager_secret.llm_key_mistral.secret_id,
    google_secret_manager_secret.aws_access_key_id.secret_id,
    google_secret_manager_secret.aws_secret_access_key.secret_id,
    google_secret_manager_secret.routellm_openai_key.secret_id,
    google_secret_manager_secret.langfuse_public_key.secret_id,
    google_secret_manager_secret.langfuse_secret_key.secret_id,
    google_secret_manager_secret.langfuse_nextauth_secret.secret_id,
    # grafana-svc + token-proxy also run as the proxy SA and mount these
    # (grafana: GF_SECURITY_ADMIN_PASSWORD; proxy: METRICS_SCRAPE_TOKEN) — without
    # them the Cloud Run deploy fails with "Permission denied on secret".
    google_secret_manager_secret.grafana_admin_password.secret_id,
    google_secret_manager_secret.metrics_scrape_token.secret_id,
    # qdrant-svc + token-proxy + docs-seed-job all run as the proxy SA and mount
    # the Qdrant app-layer key (QDRANT__SERVICE__API_KEY resp. QDRANT_API_KEY).
    google_secret_manager_secret.qdrant_api_key.secret_id,
  ], [for s in google_secret_manager_secret.llm_key_extra : s.secret_id]) : []
}

resource "google_project_iam_member" "proxy_secret_accessor" {
  count   = var.least_privilege_secret_iam ? 0 : 1
  project = var.project_id
  role    = "roles/secretmanager.secretAccessor"
  member  = "serviceAccount:${google_service_account.proxy_sa.email}"
}

resource "google_secret_manager_secret_iam_member" "proxy_per_secret_accessor" {
  for_each  = toset(local.proxy_secret_ids)
  secret_id = each.value
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${google_service_account.proxy_sa.email}"
}

# Item 7 (opt-in): narrow storage from project-wide objectAdmin to the config bucket only.
resource "google_project_iam_member" "proxy_storage_admin" {
  count   = var.least_privilege_secret_iam ? 0 : 1
  project = var.project_id
  role    = "roles/storage.objectAdmin"
  member  = "serviceAccount:${google_service_account.proxy_sa.email}"
}

resource "google_storage_bucket_iam_member" "proxy_config_bucket" {
  count = var.least_privilege_secret_iam ? 1 : 0
  # Must reference the SAME computed name as the bucket resource (local.bucket_name,
  # line 71) — NOT the raw var.config_bucket_name, which is "" by default and makes
  # this binding fail with `Import id "" doesn't match any accepted format`.
  bucket = local.bucket_name
  role   = "roles/storage.objectAdmin"
  member = "serviceAccount:${google_service_account.proxy_sa.email}"
}

# ─── Per-tenant document ingestion — SHARED scaffolding ──────────────────────
# Per-tenant doc BUCKETS are NOT provisioned here — they are created at customer
# onboarding time (api/tenant_provisioning.py) so a new tenant can ingest immediately
# without a terraform apply. Terraform provisions only the shared prerequisites every
# tenant bucket plugs into: the signBlob grant, one ingest topic, one push subscription
# to /ingest-doc, and the GCS→topic publish grant. Bucket naming (token-opt-docs-<tenant>)
# is kept in sync with tenancy/context.py::tenant_to_bucket via var.doc_bucket_prefix.
data "google_project" "this" {
  project_id = var.project_id
}

# signBlob so the proxy SA can mint V4 signed upload URLs (api/upload.py) without a key file.
resource "google_service_account_iam_member" "proxy_sign_blob" {
  service_account_id = google_service_account.proxy_sa.name
  role               = "roles/iam.serviceAccountTokenCreator"
  member             = "serviceAccount:${google_service_account.proxy_sa.email}"
}

# Dedicated identity for the Pub/Sub push → /ingest-doc (OIDC-verified by the webhook).
resource "google_service_account" "ingest_push_sa" {
  account_id   = "token-opt-ingest-push-sa"
  display_name = "Token Optimisation doc-ingest push"
}

# One shared topic; every tenant bucket's OBJECT_FINALIZE notification targets it.
resource "google_pubsub_topic" "doc_ingest" {
  name = "token-opt-doc-ingest"
}

# Allow the GCS service agent to publish object notifications onto the shared topic.
resource "google_pubsub_topic_iam_member" "gcs_publish" {
  topic  = google_pubsub_topic.doc_ingest.id
  role   = "roles/pubsub.publisher"
  member = "serviceAccount:service-${data.google_project.this.number}@gs-project-accounts.iam.gserviceaccount.com"
}

# Push subscription → the hardened /ingest-doc webhook, authenticated by an OIDC token
# minted for the push SA (the webhook checks INGEST_PUSH_SA_EMAIL + audience).
resource "google_pubsub_subscription" "doc_ingest_push" {
  count = var.proxy_service_url != "" ? 1 : 0
  name  = "token-opt-doc-ingest-push"
  topic = google_pubsub_topic.doc_ingest.id

  push_config {
    push_endpoint = "${var.proxy_service_url}/ingest-doc"
    oidc_token {
      service_account_email = google_service_account.ingest_push_sa.email
      audience              = "${var.proxy_service_url}/ingest-doc"
    }
  }

  ack_deadline_seconds       = 60
  message_retention_duration = "86400s"
}

# ─── Item 6 (opt-in): Cloud KMS envelope for the BYOK master key ──────────────
# When enabled, the master key is stored KMS-wrapped in Secret Manager and unwrapped at
# startup by the proxy (set TENANT_KEY_KMS_KEY to google_kms_crypto_key.master_key.id).
# The decrypt grant is an independent, revocable IAM role — a proxy RCE alone no longer
# yields the plaintext master key without also holding this KMS permission.
resource "google_kms_key_ring" "byok" {
  count      = var.enable_kms_master_key ? 1 : 0
  name       = "token-opt-byok"
  location   = var.region
  depends_on = [google_project_service.apis]
}

resource "google_kms_crypto_key" "master_key" {
  count           = var.enable_kms_master_key ? 1 : 0
  name            = "tenant-key-encryption"
  key_ring        = google_kms_key_ring.byok[0].id
  rotation_period = "7776000s" # 90 days
  lifecycle {
    prevent_destroy = true
  }
}

resource "google_kms_crypto_key_iam_member" "proxy_master_key_decrypter" {
  count         = var.enable_kms_master_key ? 1 : 0
  crypto_key_id = google_kms_crypto_key.master_key[0].id
  role          = "roles/cloudkms.cryptoKeyDecrypter"
  member        = "serviceAccount:${google_service_account.proxy_sa.email}"
}

resource "google_project_iam_member" "proxy_cloudsql_client" {
  project = var.project_id
  role    = "roles/cloudsql.client"
  member  = "serviceAccount:${google_service_account.proxy_sa.email}"
}


resource "google_project_iam_member" "proxy_run_invoker" {
  project = var.project_id
  role    = "roles/run.invoker"
  member  = "serviceAccount:${google_service_account.proxy_sa.email}"
}

# ─── pgvector extension (G05 L2 cache + G07 pgvector fallback) ────────────────
# The `vector` extension is needed REGARDLESS of the G07 backend: the G05 L2 semantic
# cache stores its embeddings in pgvector on Cloud SQL even when Qdrant serves G07
# (missing it → `G05 L2 pgvector error: type "vector" does not exist` on every request,
# L2 cache silently dead). G07's PGVectorRAG fallback additionally needs it when
# enable_qdrant=false.
resource "null_resource" "pgvector_extension" {
  # local-exec path applies only on the public-IP deploy — on the private-IP path the
  # in-VPC migration job (run-migrations-job.sh) applies pgvector.sql unconditionally.
  count = var.private_cloud_sql ? 0 : 1
  triggers = {
    schema_version = "1"
  }
  # Private-IP instance: tunnel via cloud-sql-proxy (127.0.0.1:6547) + psql
  # instead of `gcloud sql connect` (which needs a public IP).
  provisioner "local-exec" {
    interpreter = ["bash", "-c"] # bash heredoc/$(...) — not cmd.exe (Windows local-exec default)
    command     = <<-SHELL
      DB_PASS=$(gcloud secrets versions access latest \
        --secret="${google_secret_manager_secret.db_password.secret_id}" \
        --project="${var.project_id}" 2>/dev/null)
      cloud-sql-proxy --port 6547 "${google_sql_database_instance.main.connection_name}" \
        >/tmp/tf_sqlproxy_pgvector.log 2>&1 &
      PROXY_PID=$!
      trap 'kill "$PROXY_PID" 2>/dev/null || true' EXIT
      # Wait for the tunnel to accept connections (max ~15s).
      for _ in $(seq 1 30); do
        if (exec 3<>/dev/tcp/127.0.0.1/6547) 2>/dev/null; then exec 3>&- 3<&-; break; fi
        sleep 0.5
      done
      (exec 3<>/dev/tcp/127.0.0.1/6547) 2>/dev/null && exec 3>&- 3<&- || \
        { cat /tmp/tf_sqlproxy_pgvector.log; echo "cloud-sql-proxy did not open port 6547" >&2; exit 1; }
      PGPASSWORD="$DB_PASS" psql -h 127.0.0.1 -p 6547 \
        -U token_opt_app -d token_opt -v ON_ERROR_STOP=1 <<'EOSQL'
CREATE EXTENSION IF NOT EXISTS vector;
EOSQL
    SHELL
  }
  depends_on = [
    google_sql_database.proxy,
    google_sql_user.app_user,
    google_secret_manager_secret_version.db_password,
  ]
}

# ─── Memorystore Redis (G5 cache, G10 state, G13 streams, G18 turn KPI) ──────
resource "google_project_service" "redis_api" {
  service            = "redis.googleapis.com"
  disable_on_destroy = false
}

resource "google_redis_instance" "cache" {
  count          = var.redis_backend == "memorystore" ? 1 : 0
  name           = "token-opt-redis"
  tier           = var.redis_tier
  memory_size_gb = var.redis_memory_size_gb
  region         = var.region

  redis_version = "REDIS_7_0"

  labels = {
    environment = var.environment
    component   = "token-opt"
  }

  depends_on = [google_project_service.redis_api]
}

# ─── Docker-Redis on a GCE Container-Optimized-OS VM (redis_backend = docker) ──
# Cheaper alternative to Memorystore (~$8/mo vs ~$40). Cloud Run reaches it over
# the VPC via Direct VPC egress (default network). An ephemeral external IP is
# attached solely so COS can pull the redis image; port 6379 is exposed ONLY to
# internal source ranges by the firewall rule below. Not HA / not persistent —
# fine for cache/rate-limit/state at low scale; use redis_backend=memorystore for HA.
resource "google_compute_instance" "redis" {
  count        = var.redis_backend == "docker" ? 1 : 0
  name         = "token-opt-redis-vm"
  machine_type = var.redis_vm_machine_type
  zone         = "${var.region}-a"

  tags = ["token-opt-redis"]

  boot_disk {
    initialize_params {
      image = "cos-cloud/cos-stable"
      size  = 10
    }
  }

  network_interface {
    network = "default"
    access_config {} # ephemeral external IP for image pull only
  }

  metadata = {
    gce-container-declaration = <<-EOT
      spec:
        containers:
          - name: redis
            image: redis:7-alpine
            args: ["redis-server", "--appendonly", "no", "--save", ""]
        restartPolicy: Always
    EOT
    google-logging-enabled    = "true"
  }

  labels = {
    environment  = var.environment
    component    = "token-opt"
    container-vm = "cos-stable"
  }

  depends_on = [google_project_service.apis]
}

# Allow Redis (6379) only from internal source ranges (VPC + Direct VPC egress).
resource "google_compute_firewall" "redis_internal" {
  count   = var.redis_backend == "docker" ? 1 : 0
  name    = "token-opt-redis-internal"
  network = "default"

  allow {
    protocol = "tcp"
    ports    = ["6379"]
  }

  # RFC1918 internal ranges (Cloud Run Direct VPC egress uses VPC-internal IPs).
  source_ranges = ["10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16"]
  target_tags   = ["token-opt-redis"]

  depends_on = [google_project_service.apis]
}

# ─── Qdrant (G03 doc-pipeline ingestion, G07 hybrid search) ──────────────────
# Qdrant app-layer API key (defense-in-depth, generated once at provision time —
# never in .env/git). Layer 1 is Cloud Run IAM (ingress ALL + run.invoker required:
# unauthenticated hits are rejected at Google's front end and never reach the
# container). Layer 2 is this key, enforced by Qdrant itself — so even a
# misconfigured IAM grant (e.g. a lingering allUsers from a seeding window)
# exposes nothing. Created unconditionally (a secret is ~free) so the proxy's
# QDRANT_API_KEY mount never dangles when enable_qdrant is toggled.
resource "random_password" "qdrant_api_key" {
  length  = 48
  special = false # header-safe (sent as the `api-key` HTTP header)
}

resource "google_secret_manager_secret" "qdrant_api_key" {
  secret_id = "qdrant-api-key"
  replication {
    auto {}
  }
  depends_on = [google_project_service.apis]
}

resource "google_secret_manager_secret_version" "qdrant_api_key" {
  secret      = google_secret_manager_secret.qdrant_api_key.id
  secret_data = random_password.qdrant_api_key.result
}

resource "google_cloud_run_v2_service" "qdrant" {
  count    = var.enable_qdrant ? 1 : 0
  name     = "token-opt-qdrant"
  location = var.region
  # INGRESS_TRAFFIC_ALL + IAM (no allUsers binding anywhere): callers must present a
  # Google-signed identity token for a run.invoker principal (the proxy SA holds a
  # project-level grant). INTERNAL_ONLY is NOT usable here: Cloud Run→Cloud Run calls
  # to a run.app URL only count as "internal" with --vpc-egress=all-traffic, which
  # would force ALL proxy egress (incl. OpenAI) through the VPC and require Cloud NAT.
  # Verified empirically 2026-07-16: with private-ranges-only egress + INTERNAL_ONLY,
  # ZERO proxy/job requests ever reached this service (rejected at the GFE).
  ingress = "INGRESS_TRAFFIC_ALL"

  template {
    service_account = google_service_account.proxy_sa.email

    containers {
      image = var.qdrant_image

      ports {
        container_port = 6333
      }

      # App-layer auth (layer 2 behind Cloud Run IAM) — Qdrant rejects requests
      # without this key even if an IAM misconfiguration lets them through.
      env {
        name = "QDRANT__SERVICE__API_KEY"
        value_source {
          secret_key_ref {
            secret  = google_secret_manager_secret.qdrant_api_key.secret_id
            version = "latest"
          }
        }
      }

      # Startup CPU boost: give the container extra CPU during cold start so it
      # loads its collections faster (no idle-cost impact — boost applies only
      # during startup). Matches the --cpu-boost flag on the gcloud-deployed
      # request-serving services (proxy, sidecars, portal, langfuse, grafana, tika).
      resources {
        startup_cpu_boost = true
      }
    }
  }

  depends_on = [google_project_service.apis, google_secret_manager_secret_version.qdrant_api_key]
}

# ─── RouteLLM Sidecar Service Account ───────────────────────────────────────
resource "google_service_account" "routellm_sa" {
  account_id   = "routellm-sidecar-sa"
  display_name = "RouteLLM Sidecar"
}

resource "google_secret_manager_secret_iam_member" "routellm_secret_accessor" {
  secret_id = google_secret_manager_secret.routellm_openai_key.secret_id
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${google_service_account.routellm_sa.email}"
}

resource "google_project_iam_member" "routellm_run_invoker" {
  project = var.project_id
  role    = "roles/run.invoker"
  member  = "serviceAccount:${google_service_account.routellm_sa.email}"
}

# Allow the proxy service account to act-as the routellm service account
# Required for: gcloud run deploy --service-account=routellm-sidecar-sa
resource "google_service_account_iam_member" "proxy_sa_acts_as_routellm_sa" {
  service_account_id = google_service_account.routellm_sa.name
  role               = "roles/iam.serviceAccountUser"
  member             = "serviceAccount:${google_service_account.proxy_sa.email}"
}

# ─── Prometheus OSS (Cloud Run v2) ─────────────────────────────────────────

locals {
  prom_proxy_host        = var.proxy_service_url != "" ? trimprefix(trimprefix(var.proxy_service_url, "https://"), "http://") : "localhost:9090"
  prom_alertmanager_host = var.alertmanager_url != "" ? trimprefix(trimprefix(var.alertmanager_url, "https://"), "http://") : "localhost:9093"

  prometheus_config = templatefile("${path.module}/prometheus.yml.tmpl", {
    proxy_host           = local.prom_proxy_host
    environment          = var.environment
    alertmanager_host    = local.prom_alertmanager_host
    metrics_scrape_token = var.metrics_scrape_token
  })
}

resource "google_secret_manager_secret" "prometheus_config" {
  secret_id = "token-opt-prometheus-config"
  replication {
    auto {}
  }
  depends_on = [google_project_service.apis]
}

resource "google_secret_manager_secret_version" "prometheus_config" {
  secret      = google_secret_manager_secret.prometheus_config.id
  secret_data = local.prometheus_config
}

# H2: bearer token guarding the proxy's /metrics endpoint. The proxy reads it via
# --set-secrets METRICS_SCRAPE_TOKEN; Prometheus presents it in its scrape config
# (rendered from var.metrics_scrape_token above). A version is only created when a
# token is configured — otherwise gcp-deploy.sh leaves /metrics open.
resource "google_secret_manager_secret" "metrics_scrape_token" {
  secret_id = "token-opt-metrics-scrape-token"
  replication {
    auto {}
  }
  depends_on = [google_project_service.apis]
}

resource "google_secret_manager_secret_version" "metrics_scrape_token" {
  count       = var.metrics_scrape_token != "" ? 1 : 0
  secret      = google_secret_manager_secret.metrics_scrape_token.id
  secret_data = var.metrics_scrape_token
}

resource "google_secret_manager_secret" "prometheus_alerts" {
  secret_id = "token-opt-prometheus-alerts"
  replication {
    auto {}
  }
  depends_on = [google_project_service.apis]
}

resource "google_secret_manager_secret_version" "prometheus_alerts" {
  secret      = google_secret_manager_secret.prometheus_alerts.id
  secret_data = file("${path.module}/prometheus-alerts.yml")
}

resource "google_cloud_run_v2_service" "prometheus" {
  count    = var.enable_self_hosted_observability ? 1 : 0
  name     = "token-opt-prometheus"
  location = var.region
  ingress  = "INGRESS_TRAFFIC_INTERNAL_ONLY"

  template {
    service_account = google_service_account.proxy_sa.email

    containers {
      image = "prom/prometheus:v2.53.0"

      ports {
        container_port = 9090
      }

      args = [
        "--config.file=/secrets/prometheus/prometheus.yml",
        "--storage.tsdb.retention.time=${var.prometheus_retention}",
        "--web.enable-lifecycle",
        "--enable-feature=remote-write-receiver",
        "--web.listen-address=0.0.0.0:9090",
        "--storage.tsdb.path=/tmp/prometheus",
      ]

      startup_probe {
        initial_delay_seconds = 5
        timeout_seconds       = 5
        period_seconds        = 10
        failure_threshold     = 30
        tcp_socket {
          port = 9090
        }
      }

      volume_mounts {
        name       = "prometheus-config"
        mount_path = "/secrets/prometheus"
      }
      volume_mounts {
        name       = "prometheus-alerts"
        mount_path = "/secrets/alerts"
      }
    }

    volumes {
      name = "prometheus-config"
      secret {
        secret = google_secret_manager_secret.prometheus_config.secret_id
        items {
          version = "latest"
          path    = "prometheus.yml"
        }
      }
    }
    volumes {
      name = "prometheus-alerts"
      secret {
        secret = google_secret_manager_secret.prometheus_alerts.secret_id
        items {
          version = "latest"
          path    = "prometheus-alerts.yml"
        }
      }
    }
  }

  depends_on = [
    google_project_service.apis,
    google_secret_manager_secret_version.prometheus_config,
    google_secret_manager_secret_version.prometheus_alerts,
    google_project_iam_member.proxy_secret_accessor,
    google_secret_manager_secret_iam_member.prometheus_config_accessor,
    google_secret_manager_secret_iam_member.prometheus_alerts_accessor,
  ]
}

# ─── Alertmanager OSS (Cloud Run v2) ────────────────────────────────────────

resource "google_secret_manager_secret" "alertmanager_config" {
  secret_id = "token-opt-alertmanager-config"
  replication {
    auto {}
  }
  depends_on = [google_project_service.apis]
}

resource "google_secret_manager_secret_version" "alertmanager_config" {
  secret      = google_secret_manager_secret.alertmanager_config.id
  secret_data = <<EOF
global:
  resolve_timeout: ${var.alertmanager_resolve_timeout}

route:
  group_by: ['alertname', 'team', 'feature']
  group_wait: ${var.alertmanager_group_wait}
  group_interval: ${var.alertmanager_group_interval}
  repeat_interval: ${var.alertmanager_repeat_interval}
  receiver: 'default'

receivers:
  - name: 'default'
    webhook_configs:
      - url: '${var.alert_webhook_url != "" ? var.alert_webhook_url : (var.proxy_service_url != "" ? "${var.proxy_service_url}/admin/alert-webhook" : "http://localhost:8000/admin/alert-webhook")}'
        send_resolved: true
EOF
}

resource "google_cloud_run_v2_service" "alertmanager" {
  count    = var.enable_self_hosted_observability ? 1 : 0
  name     = "token-opt-alertmanager"
  location = var.region
  ingress  = "INGRESS_TRAFFIC_INTERNAL_ONLY"

  template {
    service_account = google_service_account.proxy_sa.email

    containers {
      image = "prom/alertmanager:v0.27.0"

      ports {
        container_port = 9093
      }

      args = concat(
        [
          "--config.file=/secrets/alertmanager/alertmanager.yml",
          "--storage.path=/tmp/alertmanager",
          "--web.listen-address=0.0.0.0:9093",
        ],
        var.alertmanager_url != "" ? ["--web.external-url=${var.alertmanager_url}"] : []
      )

      volume_mounts {
        name       = "alertmanager-config"
        mount_path = "/secrets/alertmanager"
      }
    }

    volumes {
      name = "alertmanager-config"
      secret {
        secret = google_secret_manager_secret.alertmanager_config.secret_id
        items {
          version = "latest"
          path    = "alertmanager.yml"
        }
      }
    }
  }

  depends_on = [
    google_project_service.apis,
    google_secret_manager_secret_version.alertmanager_config,
    google_project_iam_member.proxy_secret_accessor,
  ]
}

# Allow Grafana (if deployed in same project) to invoke Prometheus
resource "google_cloud_run_v2_service_iam_member" "prometheus_grafana_invoker" {
  count    = var.enable_self_hosted_observability ? 1 : 0
  location = var.region
  name     = google_cloud_run_v2_service.prometheus[0].name
  role     = "roles/run.invoker"
  member   = "serviceAccount:${google_service_account.proxy_sa.email}"
}

# Allow Prometheus to invoke Alertmanager
resource "google_cloud_run_v2_service_iam_member" "alertmanager_prometheus_invoker" {
  count    = var.enable_self_hosted_observability ? 1 : 0
  location = var.region
  name     = google_cloud_run_v2_service.alertmanager[0].name
  role     = "roles/run.invoker"
  member   = "serviceAccount:${google_service_account.proxy_sa.email}"
}

# Secret-level IAM bindings for Prometheus secret volume mounts
resource "google_secret_manager_secret_iam_member" "prometheus_config_accessor" {
  secret_id = google_secret_manager_secret.prometheus_config.secret_id
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${google_service_account.proxy_sa.email}"
}

resource "google_secret_manager_secret_iam_member" "prometheus_alerts_accessor" {
  secret_id = google_secret_manager_secret.prometheus_alerts.secret_id
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${google_service_account.proxy_sa.email}"
}

resource "google_secret_manager_secret_iam_member" "alertmanager_config_accessor" {
  secret_id = google_secret_manager_secret.alertmanager_config.secret_id
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${google_service_account.proxy_sa.email}"
}

# Note: Cloud Run v2 service deployment is done via gcp-deploy.sh script
# This IAM binding allows the proxy to invoke the RouteLLM sidecar

