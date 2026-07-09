variable "project_id" {
  description = "GCP project ID"
  type        = string
}

variable "region" {
  description = "GCP region for all resources"
  type        = string
  default     = "asia-south1"
}

variable "db_tier" {
  description = "Cloud SQL instance tier"
  type        = string
  default     = "db-g1-small"
}

variable "artifact_registry_repo" {
  description = "Artifact Registry repository name"
  type        = string
  default     = "token-opt"
}

variable "config_bucket_name" {
  description = "GCS bucket for config files and Redis exports"
  type        = string
  default     = ""
}

variable "proxy_service_url" {
  description = "Full HTTPS URL of the deployed proxy Cloud Run service (used in prometheus.yml scrape target)"
  type        = string
  default     = ""
}

variable "alertmanager_url" {
  description = "Full HTTPS URL of the deployed Alertmanager Cloud Run service (used in prometheus.yml alerting config)"
  type        = string
  default     = ""
  validation {
    condition     = var.alertmanager_url == "" || startswith(var.alertmanager_url, "https://")
    error_message = "alertmanager_url must be empty or a valid HTTPS URL (e.g. https://token-opt-alertmanager-xxxx.run.app)."
  }
}

variable "environment" {
  description = "Deployment environment label (e.g. dev, staging, prod)"
  type        = string
  default     = "dev"
}

variable "redis_tier" {
  description = "Memorystore Redis service tier"
  type        = string
  default     = "BASIC"
}

variable "redis_memory_size_gb" {
  description = "Memorystore Redis instance memory in GB"
  type        = number
  default     = 1
}

variable "qdrant_image" {
  description = "Qdrant Docker image to deploy on Cloud Run"
  type        = string
  default     = "qdrant/qdrant:v1.9.0"
}

# ─── Cost-optimization toggles (defaults preserve current OSS behaviour) ──────
# The commercial lean deploy flips these via infra/commercial.tfvars / -var.

variable "enable_qdrant" {
  description = "Deploy the Qdrant Cloud Run service. When false, use the G07 pgvector fallback on Cloud SQL (set use_pgvector_fallback: true in config)."
  type        = bool
  default     = true
}

variable "enable_self_hosted_observability" {
  description = "Deploy the self-hosted Prometheus + Alertmanager Cloud Run services. When false, use Google Cloud Monitoring instead (see infra/commercial.tf alert policies)."
  type        = bool
  default     = true
}

# ─── BYOK provider-key security hardening (opt-in; default = current behaviour) ──
# All three default to the pre-hardening posture so `terraform apply` is a no-op on an
# existing project. Flip them on a STAGING project first (item 8 changes DB connectivity
# and can break the `gcloud sql connect` migrations), validate, then promote to prod.
variable "least_privilege_secret_iam" {
  description = "Item 7: replace the proxy SA's project-wide secretmanager.secretAccessor with per-secret bindings (least privilege). Default false keeps the broad grant."
  type        = bool
  default     = false
}

variable "private_cloud_sql" {
  description = "Item 8: put Cloud SQL on a private IP (no public IPv4) + ENCRYPTED_ONLY SSL via Private Service Access. Default false keeps public IPv4. NOTE: the `gcloud sql connect` migration steps need a public/authorized path — enable the connector or run migrations from within the VPC before turning this on."
  type        = bool
  default     = false
}

variable "enable_kms_master_key" {
  description = "Item 6: provision a Cloud KMS key ring + key and grant the proxy SA cryptoKeyDecrypter, so the BYOK master key can be stored KMS-wrapped and unwrapped at startup (set TENANT_KEY_KMS_KEY on the proxy). Default false keeps the plaintext Secret Manager master key."
  type        = bool
  default     = false
}

variable "redis_backend" {
  description = "Redis backend: 'memorystore' (managed Memorystore) or 'docker' (redis container on a small GCE COS VM, cheaper)."
  type        = string
  default     = "memorystore"
  validation {
    condition     = contains(["memorystore", "docker"], var.redis_backend)
    error_message = "redis_backend must be 'memorystore' or 'docker'."
  }
}

variable "redis_vm_machine_type" {
  description = "Machine type for the docker-Redis GCE VM (redis_backend = docker)."
  type        = string
  default     = "e2-micro"
}

variable "alert_webhook_url" {
  description = "Alertmanager webhook URL override. When set, used instead of the auto-derived proxy service URL."
  type        = string
  default     = ""
}

variable "prometheus_retention" {
  description = "Prometheus TSDB retention period (e.g. 15d, 30d, 90d)"
  type        = string
  default     = "15d"
}

variable "alertmanager_resolve_timeout" {
  description = "Alertmanager global resolve_timeout (e.g. 5m, 10m)"
  type        = string
  default     = "5m"
}

variable "alertmanager_group_wait" {
  description = "Alertmanager route group_wait — initial delay before firing first alert"
  type        = string
  default     = "10s"
}

variable "alertmanager_group_interval" {
  description = "Alertmanager route group_interval — interval between batched alert notifications"
  type        = string
  default     = "10s"
}

variable "alertmanager_repeat_interval" {
  description = "Alertmanager route repeat_interval — interval before re-sending an unresolved alert"
  type        = string
  default     = "12h"
}

variable "metrics_scrape_token" {
  description = "Bearer token the proxy requires on /metrics (H2). Prometheus presents it on scrape. Empty = /metrics stays open (not recommended in production)."
  type        = string
  default     = ""
  sensitive   = true
}
