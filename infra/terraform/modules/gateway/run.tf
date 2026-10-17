# ---- API: Cloud Run v2 service ------------------------------------------------------------------------
resource "google_cloud_run_v2_service" "api" {
  project              = var.project_id
  name                 = "${local.prefix}-api"
  location             = var.region
  ingress              = var.ingress
  invoker_iam_disabled = var.public_invoker
  deletion_protection  = var.deletion_protection
  labels               = local.labels
  # Fixed audience for CD smoke-test identity tokens: valid for the main URL and tagged (canary) URLs.
  custom_audiences = ["${local.prefix}-smoke"]

  scaling {
    min_instance_count = var.min_instances
    max_instance_count = var.max_instances
  }

  template {
    service_account                  = google_service_account.api.email
    timeout                          = "${var.request_timeout_s}s"
    max_instance_request_concurrency = var.concurrency
    execution_environment            = "EXECUTION_ENVIRONMENT_GEN2"
    labels                           = local.labels

    vpc_access {
      egress = "PRIVATE_RANGES_ONLY" # Redis over the VPC; provider APIs (Groq, ...) over the internet
      network_interfaces {
        network    = google_compute_network.vpc.id
        subnetwork = google_compute_subnetwork.run.id
      }
    }

    containers {
      name  = "gateway"
      image = var.image

      ports {
        container_port = 8103
      }

      resources {
        limits = {
          cpu    = var.api_cpu
          memory = var.api_memory
        }
        # Instance-based billing: the breaker ticker and the GMP sidecar need CPU between requests, and
        # min instances > 0 requires it.
        cpu_idle          = false
        startup_cpu_boost = true
      }

      dynamic "env" {
        for_each = merge(local.common_env, {
          GATEWAY_ROLE = "api"
          GATEWAY_HOST = "0.0.0.0"
          METRICS_PORT = "8080" # localhost-only metrics server, scraped by the GMP sidecar
          METRICS_BIND = "127.0.0.1"
        })
        content {
          name  = env.key
          value = env.value
        }
      }

      dynamic "env" {
        for_each = local.api_secret_env
        content {
          name = env.key
          value_source {
            secret_key_ref {
              secret  = google_secret_manager_secret.s[env.value].secret_id
              version = "latest"
            }
          }
        }
      }

      startup_probe {
        http_get {
          path = "/readyz"
          port = 8103
        }
        initial_delay_seconds = 2
        period_seconds        = 5
        timeout_seconds       = 3
        failure_threshold     = 12
      }

      liveness_probe {
        http_get {
          path = "/healthz"
          port = 8103
        }
        period_seconds    = 15
        timeout_seconds   = 3
        failure_threshold = 3
      }
    }

    # Google Cloud Managed Service for Prometheus sidecar: its default RunMonitoring config scrapes
    # localhost:8080/metrics every 30 s and writes to Cloud Monitoring (PromQL-queryable).
    containers {
      name       = "collector"
      image      = var.gmp_sidecar_image
      depends_on = ["gateway"]
      resources {
        limits = {
          cpu    = "1"
          memory = "512Mi"
        }
      }
    }
  }

  lifecycle {
    # The CD workflow (gcloud) owns the image digest and the traffic split after the first apply.
    ignore_changes = [
      client,
      client_version,
      traffic,
      template[0].containers[0].image,
      template[0].revision,
    ]
  }

  depends_on = [google_secret_manager_secret_iam_member.access, google_project_iam_member.runtime]
}

# ---- deferrable-queue worker: Cloud Run worker pool (always on, no ingress, manual scaling) -----------
resource "google_cloud_run_v2_worker_pool" "worker" {
  project             = var.project_id
  name                = "${local.prefix}-worker"
  location            = var.region
  deletion_protection = var.deletion_protection
  labels              = local.labels

  scaling {
    scaling_mode          = "MANUAL"
    manual_instance_count = var.worker_instances
  }

  template {
    service_account = google_service_account.worker.email
    labels          = local.labels

    vpc_access {
      egress = "PRIVATE_RANGES_ONLY"
      network_interfaces {
        network    = google_compute_network.vpc.id
        subnetwork = google_compute_subnetwork.run.id
      }
    }

    containers {
      name  = "worker"
      image = var.image

      resources {
        limits = {
          cpu    = "1"
          memory = "512Mi"
        }
      }

      dynamic "env" {
        for_each = merge(local.common_env, {
          GATEWAY_ROLE = "worker"
          GATEWAY_HOST = "127.0.0.1"
          GATEWAY_PORT = "8103" # health endpoints for the probes only; worker pools have no ingress
        })
        content {
          name  = env.key
          value = env.value
        }
      }

      dynamic "env" {
        for_each = local.worker_secret_env
        content {
          name = env.key
          value_source {
            secret_key_ref {
              secret  = google_secret_manager_secret.s[env.value].secret_id
              version = "latest"
            }
          }
        }
      }

      startup_probe {
        http_get {
          path = "/readyz"
          port = 8103
        }
        period_seconds    = 5
        timeout_seconds   = 3
        failure_threshold = 12
      }

      liveness_probe {
        http_get {
          path = "/healthz"
          port = 8103
        }
        period_seconds    = 15
        timeout_seconds   = 3
        failure_threshold = 3
      }
    }
  }

  lifecycle {
    ignore_changes = [client, client_version, instance_splits, template[0].containers[0].image]
  }

  depends_on = [google_secret_manager_secret_iam_member.access, google_project_iam_member.runtime]
}

# ---- Redis keyspace migrations: Cloud Run job, executed by CD before each rollout ----------------------
resource "google_cloud_run_v2_job" "migrate" {
  project             = var.project_id
  name                = "${local.prefix}-migrate"
  location            = var.region
  deletion_protection = var.deletion_protection
  labels              = local.labels

  template {
    task_count = 1
    template {
      service_account = google_service_account.migrate.email
      timeout         = "300s"
      max_retries     = 1

      vpc_access {
        egress = "PRIVATE_RANGES_ONLY"
        network_interfaces {
          network    = google_compute_network.vpc.id
          subnetwork = google_compute_subnetwork.run.id
        }
      }

      containers {
        image   = var.image
        command = ["python", "-m", "gateway.migrate"]

        resources {
          limits = {
            cpu    = "1"
            memory = "512Mi"
          }
        }

        dynamic "env" {
          for_each = { ENV = var.env, REDIS_URL = local.redis_url, REDIS_CA_CERT_PEM = local.redis_ca }
          content {
            name  = env.key
            value = env.value
          }
        }

        dynamic "env" {
          for_each = local.migrate_secret_env
          content {
            name = env.key
            value_source {
              secret_key_ref {
                secret  = google_secret_manager_secret.s[env.value].secret_id
                version = "latest"
              }
            }
          }
        }
      }
    }
  }

  lifecycle {
    ignore_changes = [client, client_version, template[0].template[0].containers[0].image]
  }

  depends_on = [google_secret_manager_secret_iam_member.access, google_project_iam_member.runtime]
}
