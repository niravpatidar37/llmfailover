output "service_name" {
  description = "Cloud Run API service name."
  value       = google_cloud_run_v2_service.api.name
}

output "service_url" {
  description = "Cloud Run API service URL."
  value       = google_cloud_run_v2_service.api.uri
}

output "worker_pool_name" {
  description = "Cloud Run worker pool running the deferrable queue."
  value       = google_cloud_run_v2_worker_pool.worker.name
}

output "migrate_job_name" {
  description = "Cloud Run job that applies Redis keyspace migrations."
  value       = google_cloud_run_v2_job.migrate.name
}

output "artifact_repository" {
  description = "Docker repository path for images."
  value       = "${var.region}-docker.pkg.dev/${var.project_id}/${google_artifact_registry_repository.images.repository_id}"
}

output "redis_instance" {
  description = "Memorystore instance name (copy its AUTH string into the redis-auth secret; see RUNBOOK)."
  value       = google_redis_instance.cache.name
}

output "secret_ids" {
  description = "Secret Manager secret ids to populate with `gcloud secrets versions add`."
  value       = { for k, s in google_secret_manager_secret.s : k => s.secret_id }
}

output "runtime_service_accounts" {
  description = "Runtime service accounts per process role."
  value       = local.runtime_sas
}
