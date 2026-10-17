output "service_url" {
  description = "API service URL."
  value       = module.gateway.service_url
}

output "service_name" {
  description = "API service name."
  value       = module.gateway.service_name
}

output "worker_pool_name" {
  description = "Worker pool name."
  value       = module.gateway.worker_pool_name
}

output "migrate_job_name" {
  description = "Migration job name."
  value       = module.gateway.migrate_job_name
}

output "artifact_repository" {
  description = "Docker repository path."
  value       = module.gateway.artifact_repository
}

output "redis_instance" {
  description = "Memorystore instance name."
  value       = module.gateway.redis_instance
}

output "secret_ids" {
  description = "Secrets to populate out of band."
  value       = module.gateway.secret_ids
}
