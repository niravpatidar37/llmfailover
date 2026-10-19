# One least-privilege service account per process role.
resource "google_service_account" "api" {
  project      = var.project_id
  account_id   = "${substr(local.prefix, 0, 22)}-api"
  display_name = "LLM gateway API (${var.env})"
}

resource "google_service_account" "worker" {
  project      = var.project_id
  account_id   = "${substr(local.prefix, 0, 22)}-wrk"
  display_name = "LLM gateway deferrable-queue worker (${var.env})"
}

resource "google_service_account" "migrate" {
  project      = var.project_id
  account_id   = "${substr(local.prefix, 0, 22)}-mig"
  display_name = "LLM gateway Redis keyspace migrations (${var.env})"
}

locals {
  runtime_sas = {
    api     = google_service_account.api.email
    worker  = google_service_account.worker.email
    migrate = google_service_account.migrate.email
  }
  # Which secrets each role may read (secret-level grants, not project-wide).
  secret_grants = merge(
    { for k, s in local.api_secret_env : "api-${s}" => { sa = "api", secret = s } },
    { for k, s in local.worker_secret_env : "worker-${s}" => { sa = "worker", secret = s } },
    { for k, s in local.migrate_secret_env : "migrate-${s}" => { sa = "migrate", secret = s } },
  )
  project_roles = {
    "api-log"     = { sa = "api", role = "roles/logging.logWriter" }
    "api-metrics" = { sa = "api", role = "roles/monitoring.metricWriter" } # GMP sidecar writes metrics
    "worker-log"  = { sa = "worker", role = "roles/logging.logWriter" }
    "migrate-log" = { sa = "migrate", role = "roles/logging.logWriter" }
  }
}

resource "google_secret_manager_secret_iam_member" "access" {
  for_each  = local.secret_grants
  project   = var.project_id
  secret_id = google_secret_manager_secret.s[each.value.secret].secret_id
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${local.runtime_sas[each.value.sa]}"
}

resource "google_project_iam_member" "runtime" {
  for_each = local.project_roles
  project  = var.project_id
  role     = each.value.role
  member   = "serviceAccount:${local.runtime_sas[each.value.sa]}"
}

# ---- deploy identity (GitHub Actions via Workload Identity Federation) --------------------------------
resource "google_artifact_registry_repository_iam_member" "deploy_push" {
  project    = var.project_id
  location   = google_artifact_registry_repository.images.location
  repository = google_artifact_registry_repository.images.name
  role       = "roles/artifactregistry.writer"
  member     = "serviceAccount:${var.deploy_service_account_email}"
}

resource "google_project_iam_member" "deploy_run" {
  project = var.project_id
  role    = "roles/run.developer"
  member  = "serviceAccount:${var.deploy_service_account_email}"
}

resource "google_service_account_iam_member" "deploy_act_as" {
  for_each           = local.runtime_sas
  service_account_id = "projects/${var.project_id}/serviceAccounts/${each.value}"
  role               = "roles/iam.serviceAccountUser"
  member             = "serviceAccount:${var.deploy_service_account_email}"
}

# The CD smoke test (scripts/smoke.py) reads the smoke tenant's key; this is the deploy SA's only secret.
resource "google_secret_manager_secret_iam_member" "deploy_smoke_key" {
  project   = var.project_id
  secret_id = google_secret_manager_secret.s["smoke_api_key"].secret_id
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${var.deploy_service_account_email}"
}

# Smoke tests call the service with an identity token when it is not public.
resource "google_cloud_run_v2_service_iam_member" "deploy_invoke" {
  project  = var.project_id
  location = google_cloud_run_v2_service.api.location
  name     = google_cloud_run_v2_service.api.name
  role     = "roles/run.invoker"
  member   = "serviceAccount:${var.deploy_service_account_email}"
}
