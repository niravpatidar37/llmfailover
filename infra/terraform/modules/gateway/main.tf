locals {
  labels = merge({ app = var.name, env = var.env, managed-by = "terraform" }, var.labels)
  prefix = "${var.name}-${var.env}"

  apis = [
    "artifactregistry.googleapis.com",
    "compute.googleapis.com",
    "iam.googleapis.com",
    "logging.googleapis.com",
    "monitoring.googleapis.com",
    "redis.googleapis.com",
    "run.googleapis.com",
    "secretmanager.googleapis.com",
  ]

  # Secret Manager secrets are created EMPTY: values are added out of band with
  # `gcloud secrets versions add` (docs/RUNBOOK.md), so no secret value is ever in Terraform code or state.
  secrets = {
    groq_api_key      = "${local.prefix}-groq-api-key"
    anthropic_api_key = "${local.prefix}-anthropic-api-key"
    hf_token          = "${local.prefix}-hf-token"
    admin_token       = "${local.prefix}-admin-token"
    tenant_keys       = "${local.prefix}-tenant-keys"
    redis_auth        = "${local.prefix}-redis-auth"
  }

  provider_secret_env = merge(
    { GROQ_API_KEY = "groq_api_key" },
    var.enable_anthropic ? { ANTHROPIC_API_KEY = "anthropic_api_key" } : {},
    var.enable_hf ? { HF_TOKEN = "hf_token" } : {},
  )
  # env var name -> key in local.secrets (references only; values never pass through Terraform)
  redis_secret_env = zipmap(["REDIS_PASSWORD"], ["redis_auth"])
  api_secret_env = merge(
    local.provider_secret_env,
    local.redis_secret_env,
    zipmap(["GATEWAY_ADMIN_TOKEN", "TENANT_KEYS_JSON"], ["admin_token", "tenant_keys"]),
  )
  worker_secret_env  = merge(local.provider_secret_env, local.redis_secret_env)
  migrate_secret_env = local.redis_secret_env

  redis_url = "rediss://${google_redis_instance.cache.host}:${google_redis_instance.cache.port}/0"
  redis_ca  = google_redis_instance.cache.server_ca_certs[0].cert

  # Plain (non-secret) env shared by every process role.
  common_env = merge(
    {
      ENV                   = var.env
      GATEWAY_CONFIG        = "/app/config/gateway.prod.yaml"
      SPEND_BACKEND         = "redis"
      PROJECT_SPEND_CAP_USD = tostring(var.project_spend_cap_usd)
      REDIS_URL             = local.redis_url
      REDIS_CA_CERT_PEM     = local.redis_ca
      LLM_KILL_SWITCH       = var.kill_switch ? "1" : "0"
    },
    var.enable_hf ? { HF_ENABLED = "1" } : {},
  )
}

resource "google_project_service" "apis" {
  for_each           = toset(local.apis)
  project            = var.project_id
  service            = each.value
  disable_on_destroy = false
}

# ---- Artifact Registry ---------------------------------------------------------------------------------
resource "google_artifact_registry_repository" "images" {
  #checkov:skip=CKV_GCP_84:Google-managed encryption at rest is used; images hold no secrets (they are injected at runtime from Secret Manager) and a CMEK key ring adds KMS cost and key-rotation operations this MVP does not run.
  project       = var.project_id
  location      = var.region
  repository_id = local.prefix
  format        = "DOCKER"
  description   = "Gateway images (${var.env}). Deployed by digest."
  labels        = local.labels

  docker_config {
    immutable_tags = true
  }

  cleanup_policy_dry_run = false
  cleanup_policies {
    id     = "keep-recent"
    action = "KEEP"
    most_recent_versions {
      keep_count = 20
    }
  }
  cleanup_policies {
    id     = "delete-old-untagged"
    action = "DELETE"
    condition {
      tag_state  = "UNTAGGED"
      older_than = "2592000s"
    }
  }

  depends_on = [google_project_service.apis]
}

# ---- Secrets (empty containers; values added out of band) ---------------------------------------------
resource "google_secret_manager_secret" "s" {
  for_each  = local.secrets
  project   = var.project_id
  secret_id = each.value
  labels    = local.labels

  replication {
    auto {}
  }

  depends_on = [google_project_service.apis]
}
