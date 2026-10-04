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

output "grafana_service_account_email" {
  description = "Service account grafana-svc runs as (gcp-deploy.sh), not the proxy's"
  value       = google_service_account.grafana_sa.email
}

output "langfuse_service_account_email" {
  description = "Service account langfuse-svc runs as (gcp-deploy.sh), not the proxy's"
  value       = google_service_account.langfuse_sa.email
}

output "db_password_secret_name" {
  description = "Secret Manager secret name for DB password"
  value       = google_secret_manager_secret.db_password.secret_id
}

output "byok_master_kms_key_id" {
  description = "Item 6: KMS crypto-key resource id for wrapping the BYOK master key (empty unless enable_kms_master_key). Set as TENANT_KEY_KMS_KEY on the proxy so it unwraps the master key at startup."
  value       = var.enable_kms_master_key ? google_kms_crypto_key.master_key[0].id : ""
}

output "prometheus_service_url" {
  description = "Internal Cloud Run URL for the Prometheus OSS service (empty when self-hosted observability is disabled — use Cloud Monitoring)"
  value       = var.enable_self_hosted_observability ? google_cloud_run_v2_service.prometheus[0].uri : ""
}

output "redis_host" {
  description = "Redis host — Memorystore host or the docker-Redis GCE VM internal IP (the clients use redis_url)"
  value = (
    var.redis_backend == "memorystore"
    ? google_redis_instance.cache[0].host
    : google_compute_instance.redis[0].network_interface[0].network_ip
  )
}

output "redis_url" {
  description = "REDIS_URL for the clients: rediss:// once redis_tls is on, with Memorystore's port (6378 with TLS); no credentials (the password is the redis-auth secret, the CA the redis-ca secret)"
  value = format("%s://%s:%d/0", var.redis_tls ? "rediss" : "redis",
    var.redis_backend == "memorystore" ? google_redis_instance.cache[0].host : google_compute_instance.redis[0].network_interface[0].network_ip,
  var.redis_backend == "memorystore" ? google_redis_instance.cache[0].port : 6379)
}

output "qdrant_service_url" {
  description = "Internal Cloud Run URL for the Qdrant service (empty when Qdrant is disabled — use pgvector)"
  value       = var.enable_qdrant ? google_cloud_run_v2_service.qdrant[0].uri : ""
}

output "qdrant_snapshot_bucket" {
  description = "Bucket of Qdrant's collection snapshots (QDRANT_SNAPSHOT_BUCKET for the ingest job and token-proxy; empty when Qdrant is disabled)"
  value       = var.enable_qdrant ? google_storage_bucket.qdrant_snapshots[0].name : ""
}
