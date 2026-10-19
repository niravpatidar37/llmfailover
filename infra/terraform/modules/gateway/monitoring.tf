# ---- Monitoring as code: uptime check, SLO / saturation alerts and app-specific alerts -----------------
#
# App metrics reach Cloud Monitoring through the Managed Service for Prometheus sidecar (run.tf), so the
# app-level policies are PromQL conditions (`condition_prometheus_query_language`). Platform signals (5xx
# from the Cloud Run frontend, CPU/memory, instance count, uptime) use Cloud Monitoring metric filters.
# Thresholds are variables with documented defaults; docs/SLO.md explains each one.

data "google_project" "this" {
  project_id = var.project_id
}

locals {
  api_service = google_cloud_run_v2_service.api.name
  api_host    = trimprefix(google_cloud_run_v2_service.api.uri, "https://")
  channels    = [google_monitoring_notification_channel.email.id]
  alert_label = merge(local.labels, { service = local.api_service })

  # Interactive availability SLI (docs/SLO.md): 5xx over all non-4xx responses of the interactive classes.
  interactive = "request_class=~\"classify|generate\""
  bad_ratio   = "(sum(rate(gateway_client_requests_total{%s,status=~\"5..\"}[%s])) / clamp_min(sum(rate(gateway_client_requests_total{%s,status!~\"4..\"}[%s])), 1e-9))"
  budget      = 1 - var.slo_availability_target

  run_filter = "resource.type = \"cloud_run_revision\" AND resource.labels.service_name = \"${local.api_service}\""

  # PromQL alert policies: name => {query, duration, severity, doc}
  promql_alerts = {
    "error-budget-fast-burn" = {
      display  = "Error budget fast burn (1h and 5m > 14.4x)"
      severity = "CRITICAL"
      duration = "120s"
      query = format(
        "%s > %.4f and %s > %.4f",
        format(local.bad_ratio, local.interactive, "1h", local.interactive, "1h"), 14.4 * local.budget,
        format(local.bad_ratio, local.interactive, "5m", local.interactive, "5m"), 14.4 * local.budget,
      )
      doc = "Interactive requests are burning the monthly error budget >14.4x faster than allowed (2% of the budget per hour). Page. RUNBOOK section 7 (503 providers_degraded) and the on-call checklist."
    }
    "error-budget-slow-burn" = {
      display  = "Error budget slow burn (6h and 30m > 6x)"
      severity = "ERROR"
      duration = "900s"
      query = format(
        "%s > %.4f and %s > %.4f",
        format(local.bad_ratio, local.interactive, "6h", local.interactive, "6h"), 6 * local.budget,
        format(local.bad_ratio, local.interactive, "30m", local.interactive, "30m"), 6 * local.budget,
      )
      doc = "Interactive error budget burning >6x the sustainable rate over 6 h. Ticket: look for a degraded provider or a bad rollout."
    }
    "p95-latency-over-slo" = {
      display  = "Interactive p95 latency over SLO"
      severity = "ERROR"
      duration = "600s"
      query    = "histogram_quantile(0.95, sum by (le) (rate(gateway_client_request_seconds_bucket{${local.interactive}}[5m]))) > ${var.latency_p95_slo_s}"
      doc      = "Gateway-observed p95 latency for classify/generate is above the SLO (docs/SLO.md). Check provider p95 (gateway_health_latency_seconds) and hedge outcomes."
    }
    "breaker-open" = {
      display  = "Provider circuit breaker open"
      severity = "WARNING"
      duration = "600s"
      query    = "max by (provider) (gateway_circuit_state) == 2"
      doc      = "A provider breaker has been open for 10 min. GET /admin/providers and /admin/breaker/events. With one external provider there is no failover target."
    }
    "failover-rate-high" = {
      display  = "Failover rate high"
      severity = "WARNING"
      duration = "600s"
      query    = "sum(rate(gateway_failovers_total[10m])) / clamp_min(sum(rate(gateway_client_requests_total{status!~\"4..\"}[10m])), 1e-9) > ${var.failover_rate_threshold}"
      doc      = "More than the threshold share of requests are moved off their preferred provider. The preferred provider is degraded; failover is masking it."
    }
    "queue-depth-high" = {
      display  = "Deferrable queue depth high"
      severity = "WARNING"
      duration = "900s"
      query    = "max(gateway_queue_depth) > ${var.queue_depth_threshold}"
      doc      = "Deferrable jobs are piling up: worker pool down, kill switch engaged, or all providers open. RUNBOOK section 7 (Queue depth keeps growing)."
    }
    "tenant-cost-over-budget" = {
      display  = "Tenant cost over daily budget"
      severity = "WARNING"
      duration = "60s"
      query    = "sum by (tenant) (increase(gateway_cost_usd_total[1d])) > ${var.tenant_daily_budget_usd}"
      doc      = "A tenant's attributed LLM cost (list-price estimate) over the last 24 h exceeds its budget. GET /admin/costs; lower the tenant's RPM in the tenant spec if needed. The project-wide hard cap still applies."
    }
    "failover-capacity-low" = {
      display  = "Fewer than 2 healthy external providers"
      severity = "WARNING"
      duration = "900s"
      query    = "max(gateway_external_providers_healthy) < 2"
      doc      = "No independent failover target. Expected to fire while only Groq is keyed; enable a second provider (RUNBOOK 'Enable a second provider')."
    }
    "valid-completions-low" = {
      display  = "Valid-completion ratio below SLO"
      severity = "WARNING"
      duration = "900s"
      query    = "sum(rate(gateway_completions_total{validity=\"invalid\"}[1h])) / clamp_min(sum(rate(gateway_completions_total[1h])), 1e-9) > ${format("%.4f", 1 - var.slo_valid_completion_target)}"
      doc      = "Too many completions are empty, truncated or schema-invalid (quality SLI, docs/SLO.md). Check gateway_completions_total by provider and reason."
    }
  }
}

resource "google_monitoring_notification_channel" "email" {
  project      = var.project_id
  display_name = "${local.prefix} on-call email"
  type         = "email"
  labels = {
    email_address = var.alert_email
  }
  user_labels = local.labels
  depends_on  = [google_project_service.apis]
}

# ---- uptime check on /healthz -------------------------------------------------------------------------
resource "google_monitoring_uptime_check_config" "healthz" {
  project      = var.project_id
  display_name = "${local.prefix} /healthz"
  timeout      = "10s"
  period       = "60s"
  user_labels  = local.labels

  http_check {
    path           = "/healthz"
    port           = 443
    use_ssl        = true
    validate_ssl   = true
    request_method = "GET"
    accepted_response_status_codes {
      status_value = 200
    }
    # IAM-protected services (public_invoker = false): the check sends an identity token minted for the
    # Cloud Monitoring service agent, which is granted run.invoker below.
    dynamic "service_agent_authentication" {
      for_each = var.public_invoker ? [] : [1]
      content {
        type = "OIDC_TOKEN"
      }
    }
  }

  content_matchers {
    content = "\"status\":\"ok\""
    matcher = "CONTAINS_STRING"
  }

  monitored_resource {
    type = "uptime_url"
    labels = {
      project_id = var.project_id
      host       = local.api_host
    }
  }

  depends_on = [google_project_service.apis]
}

resource "google_cloud_run_v2_service_iam_member" "uptime_invoker" {
  count    = var.public_invoker ? 0 : 1
  project  = var.project_id
  location = google_cloud_run_v2_service.api.location
  name     = google_cloud_run_v2_service.api.name
  role     = "roles/run.invoker"
  member   = "serviceAccount:service-${data.google_project.this.number}@gcp-sa-monitoring-notification.iam.gserviceaccount.com"
}

resource "google_monitoring_alert_policy" "uptime" {
  project               = var.project_id
  display_name          = "${local.prefix}: /healthz uptime check failing"
  combiner              = "OR"
  severity              = "CRITICAL"
  notification_channels = local.channels
  user_labels           = local.alert_label

  conditions {
    display_name = "uptime check failing from 2+ regions"
    condition_threshold {
      filter          = "metric.type = \"monitoring.googleapis.com/uptime_check/check_passed\" AND resource.type = \"uptime_url\" AND metric.labels.check_id = \"${google_monitoring_uptime_check_config.healthz.uptime_check_id}\""
      comparison      = "COMPARISON_GT"
      threshold_value = 1
      duration        = "300s"
      aggregations {
        alignment_period     = "1200s"
        per_series_aligner   = "ALIGN_NEXT_OLDER"
        cross_series_reducer = "REDUCE_COUNT_FALSE"
        group_by_fields      = ["resource.label.*"]
      }
      trigger {
        count = 1
      }
    }
  }

  documentation {
    content   = "Liveness (/healthz) is failing from more than one checker region. The process or the Cloud Run service is down. RUNBOOK sections 3-4 (verify, rollback)."
    mime_type = "text/markdown"
  }

  alert_strategy {
    auto_close = "1800s"
  }
}

# ---- platform: 5xx ratio and instance saturation ------------------------------------------------------
resource "google_monitoring_alert_policy" "five_xx_ratio" {
  project               = var.project_id
  display_name          = "${local.prefix}: 5xx ratio above threshold"
  combiner              = "OR"
  severity              = "ERROR"
  notification_channels = local.channels
  user_labels           = local.alert_label

  conditions {
    display_name = "Cloud Run 5xx / all requests"
    condition_threshold {
      filter             = "metric.type = \"run.googleapis.com/request_count\" AND ${local.run_filter} AND metric.labels.response_code_class = \"5xx\""
      denominator_filter = "metric.type = \"run.googleapis.com/request_count\" AND ${local.run_filter}"
      comparison         = "COMPARISON_GT"
      threshold_value    = var.five_xx_ratio_threshold
      duration           = "300s"
      aggregations {
        alignment_period     = "60s"
        per_series_aligner   = "ALIGN_RATE"
        cross_series_reducer = "REDUCE_SUM"
      }
      denominator_aggregations {
        alignment_period     = "60s"
        per_series_aligner   = "ALIGN_RATE"
        cross_series_reducer = "REDUCE_SUM"
      }
    }
  }

  documentation {
    content   = "Includes the gateway's own 503s (providers degraded, kill switch) and platform errors (crashes, timeouts). RUNBOOK section 7."
    mime_type = "text/markdown"
  }

  alert_strategy {
    auto_close = "1800s"
  }
}

resource "google_monitoring_alert_policy" "saturation" {
  project               = var.project_id
  display_name          = "${local.prefix}: instance saturation"
  combiner              = "OR"
  severity              = "WARNING"
  notification_channels = local.channels
  user_labels           = local.alert_label

  conditions {
    display_name = "container CPU p99 above saturation_threshold"
    condition_threshold {
      filter          = "metric.type = \"run.googleapis.com/container/cpu/utilizations\" AND ${local.run_filter} AND metric.labels.container_name = \"gateway\""
      comparison      = "COMPARISON_GT"
      threshold_value = var.saturation_threshold
      duration        = "600s"
      aggregations {
        alignment_period     = "300s"
        per_series_aligner   = "ALIGN_PERCENTILE_99"
        cross_series_reducer = "REDUCE_MAX"
      }
    }
  }

  conditions {
    display_name = "container memory p99 above saturation_threshold"
    condition_threshold {
      filter          = "metric.type = \"run.googleapis.com/container/memory/utilizations\" AND ${local.run_filter} AND metric.labels.container_name = \"gateway\""
      comparison      = "COMPARISON_GT"
      threshold_value = var.saturation_threshold
      duration        = "600s"
      aggregations {
        alignment_period     = "300s"
        per_series_aligner   = "ALIGN_PERCENTILE_99"
        cross_series_reducer = "REDUCE_MAX"
      }
    }
  }

  conditions {
    display_name = "instance count above saturation_threshold x max_instances"
    condition_threshold {
      filter          = "metric.type = \"run.googleapis.com/container/instance_count\" AND ${local.run_filter}"
      comparison      = "COMPARISON_GT"
      threshold_value = var.max_instances * var.saturation_threshold
      duration        = "600s"
      aggregations {
        alignment_period     = "60s"
        per_series_aligner   = "ALIGN_MAX"
        cross_series_reducer = "REDUCE_SUM"
      }
    }
  }

  documentation {
    content   = "CPU/memory near the limit or autoscaling close to max_instances. Raise api_cpu/api_memory or max_instances in tfvars, or shed load (tenant RPM)."
    mime_type = "text/markdown"
  }

  alert_strategy {
    auto_close = "1800s"
  }
}

# ---- app-specific PromQL alerts (SLO burn, latency, breaker, failover, queue, cost, capacity, quality) ---
resource "google_monitoring_alert_policy" "promql" {
  for_each              = local.promql_alerts
  project               = var.project_id
  display_name          = "${local.prefix}: ${each.value.display}"
  combiner              = "OR"
  severity              = each.value.severity
  notification_channels = local.channels
  user_labels           = local.alert_label

  conditions {
    display_name = each.value.display
    condition_prometheus_query_language {
      query               = each.value.query
      duration            = each.value.duration
      evaluation_interval = "60s"
      alert_rule          = replace(each.key, "-", "_")
      rule_group          = "llmfailover"
    }
  }

  documentation {
    content   = each.value.doc
    mime_type = "text/markdown"
  }

  alert_strategy {
    auto_close = "1800s"
  }
}
