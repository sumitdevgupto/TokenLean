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
