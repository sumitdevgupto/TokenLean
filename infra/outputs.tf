output "config_bucket_name" {
  description = "GCS bucket for config files"
  value       = google_storage_bucket.config.name
}

output "artifact_registry_url" {
  description = "Artifact Registry URL for Docker images"
  value       = "${var.region}-docker.pkg.dev/${var.project_id}/${var.artifact_registry_repo}"
}

output "db_instance_connection_name" {
  description = "Cloud SQL connection name (for Cloud SQL Auth Proxy)"
  value       = google_sql_database_instance.main.connection_name
}

output "proxy_service_account_email" {
  description = "Service account email for the proxy Cloud Run service"
  value       = google_service_account.proxy_sa.email
}

output "db_password_secret_name" {
  description = "Secret Manager secret name for DB password"
  value       = google_secret_manager_secret.db_password.secret_id
}

output "prometheus_service_url" {
  description = "Internal Cloud Run URL for the Prometheus OSS service (empty when self-hosted observability is disabled — use Cloud Monitoring)"
  value       = var.enable_self_hosted_observability ? google_cloud_run_v2_service.prometheus[0].uri : ""
}

output "redis_host" {
  description = "Redis host for REDIS_URL=redis://<host>:6379/0 — Memorystore host or the docker-Redis GCE VM internal IP"
  value = (
    var.redis_backend == "memorystore"
    ? google_redis_instance.cache[0].host
    : google_compute_instance.redis[0].network_interface[0].network_ip
  )
}

output "qdrant_service_url" {
  description = "Internal Cloud Run URL for the Qdrant service (empty when Qdrant is disabled — use pgvector)"
  value       = var.enable_qdrant ? google_cloud_run_v2_service.qdrant[0].uri : ""
}
