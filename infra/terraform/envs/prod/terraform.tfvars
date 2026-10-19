# Prod: always-on, autoscaling, HA Redis. Placeholders: replace project/image/deploy SA (RUNBOOK).
project_id                   = "CHANGE-ME-prod-project"
region                       = "us-central1"
image                        = "us-central1-docker.pkg.dev/CHANGE-ME-prod-project/llm-gateway-prod/gateway@sha256:0000000000000000000000000000000000000000000000000000000000000000"
deploy_service_account_email = "gh-deploy@CHANGE-ME-prod-project.iam.gserviceaccount.com"

# Autoscaling across instances relies on the Redis-shared breaker, limits and spend cap, proven by
# tests/test_multi_instance.py (fakeredis + real Redis 7 in CI + two OS processes). It has NOT been
# exercised on real Cloud Run (nothing is applied). Set max_instances = 1 to fall back to one instance.
min_instances = 1
max_instances = 10

redis_tier            = "STANDARD_HA"
public_invoker        = true # OpenAI-compatible clients send tenant API keys; the app authenticates them
project_spend_cap_usd = 25
deletion_protection   = true

# Only Groq is keyed today, so prod has ONE external provider (no failover target). Flip these once the
# secrets have versions (and, for Anthropic, the model id is verified via GET /v1/models).
enable_anthropic = false
enable_hf        = false

# Monitoring (modules/gateway/monitoring.tf): on-call channel and per-tenant daily cost alert.
alert_email             = "CHANGE-ME-oncall@example.com"
tenant_daily_budget_usd = 1
