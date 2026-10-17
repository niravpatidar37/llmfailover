# Staging: small, scale-to-zero, IAM-protected. Placeholders: replace project/image/deploy SA (RUNBOOK).
project_id                   = "CHANGE-ME-staging-project"
region                       = "us-central1"
image                        = "us-central1-docker.pkg.dev/CHANGE-ME-staging-project/llm-gateway-staging/gateway@sha256:0000000000000000000000000000000000000000000000000000000000000000"
deploy_service_account_email = "gh-deploy@CHANGE-ME-staging-project.iam.gserviceaccount.com"

min_instances = 0
max_instances = 3 # > 1 on purpose: staging exercises the Redis-shared breaker across instances

redis_tier            = "BASIC" # no replica/persistence: a restart loses queue, breaker and spend state
public_invoker        = false   # smoke tests use an identity token
project_spend_cap_usd = 5
deletion_protection   = false
