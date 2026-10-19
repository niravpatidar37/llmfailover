# Runbook: llmfailover on Google Cloud Run

**Status: deploy-ready; not applied (no cloud account connected).** Every command below is written against the
Terraform in `infra/terraform/` and the CD workflow in `.github/workflows/deploy.yml`, but none of it has been
run against a real project. Treat the first rollout as a rehearsal and fix anything that differs.

Resource names use `llm-gateway-<env>-*` (`<env>` = `staging` or `prod`):

| Resource | Name | Purpose |
|---|---|---|
| Cloud Run service | `llm-gateway-<env>-api` | OpenAI-compatible API (`GATEWAY_ROLE=api`), GMP sidecar `collector` |
| Cloud Run worker pool | `llm-gateway-<env>-worker` | deferrable queue worker (`GATEWAY_ROLE=worker`), always on, no ingress |
| Cloud Run job | `llm-gateway-<env>-migrate` | versioned Redis keyspace migrations (`python -m gateway.migrate`) |
| Memorystore for Redis | `llm-gateway-<env>-redis` | breakers, health windows, idempotency, queue, limits, spend counter |
| Artifact Registry | `llm-gateway-<env>` | images, deployed by digest |
| Secret Manager | `llm-gateway-<env>-{groq-api-key,anthropic-api-key,hf-token,admin-token,tenant-keys,redis-auth}` | created empty by Terraform |
| Service accounts | `llm-gateway-<env>-{api,wrk,mig}` | one least-privilege identity per role |

Shell variables used below:

```bash
export PROJECT=my-gcp-project REGION=us-central1 ENV=staging     # or ENV=prod
export P=llm-gateway-$ENV
export REPO=niravpatidar37/llmfailover
```

## 1. One-time bootstrap (per project)

```bash
gcloud config set project "$PROJECT"
PROJECT_NUMBER=$(gcloud projects describe "$PROJECT" --format='value(projectNumber)')
gcloud services enable iam.googleapis.com iamcredentials.googleapis.com sts.googleapis.com \
  cloudresourcemanager.googleapis.com storage.googleapis.com

# Terraform remote state (versioned, no public access)
gcloud storage buckets create "gs://$PROJECT-tfstate" --location="$REGION" \
  --uniform-bucket-level-access --public-access-prevention
gcloud storage buckets update "gs://$PROJECT-tfstate" --versioning

# Workload Identity Federation for GitHub Actions (keyless; only this repository can use it)
gcloud iam workload-identity-pools create github --location=global --display-name="GitHub Actions"
gcloud iam workload-identity-pools providers create-oidc gh-oidc --location=global \
  --workload-identity-pool=github --issuer-uri="https://token.actions.githubusercontent.com" \
  --attribute-mapping="google.subject=assertion.sub,attribute.repository=assertion.repository,attribute.ref=assertion.ref" \
  --attribute-condition="assertion.repository=='$REPO'"
gcloud iam service-accounts create gh-deploy --display-name="GitHub Actions deploy"
DEPLOY_SA="gh-deploy@$PROJECT.iam.gserviceaccount.com"
PRINCIPAL="principalSet://iam.googleapis.com/projects/$PROJECT_NUMBER/locations/global/workloadIdentityPools/github/attribute.repository/$REPO"
gcloud iam service-accounts add-iam-policy-binding "$DEPLOY_SA" --role=roles/iam.workloadIdentityUser --member="$PRINCIPAL"
# The deploy jobs mint an identity token for the smoke tests:
gcloud iam service-accounts add-iam-policy-binding "$DEPLOY_SA" --role=roles/iam.serviceAccountTokenCreator --member="$PRINCIPAL"

# GitHub: repository variables (not secrets; nothing secret is stored in GitHub) and environments
gh variable set GCP_PROJECT_ID   -R "$REPO" -b "$PROJECT"
gh variable set GCP_REGION       -R "$REPO" -b "$REGION"
gh variable set GCP_WIF_PROVIDER -R "$REPO" -b "projects/$PROJECT_NUMBER/locations/global/workloadIdentityPools/github/providers/gh-oidc"
gh variable set GCP_DEPLOY_SA    -R "$REPO" -b "$DEPLOY_SA"
gh api -X PUT "repos/$REPO/environments/staging"
# production: add yourself as a required reviewer (the manual approval gate for the prod job)
gh api -X PUT "repos/$REPO/environments/production" --input - <<EOF
{"reviewers":[{"type":"User","id":$(gh api users/niravpatidar37 -q .id)}]}
EOF
```

Setting `GCP_PROJECT_ID` turns the deploy jobs on. Until the first `terraform apply` below has finished, leave
it unset (the jobs update resources that must already exist).

## 2. First apply (per environment)

Terraform is run by a human with project Owner (or an equivalent admin) the first time; the deploy SA only
gets `run.developer`, `iam.serviceAccountUser` on the runtime SAs, `run.invoker`, and AR writer.

```bash
cd infra/terraform/envs/$ENV
# edit terraform.tfvars: project_id, deploy_service_account_email (and the repo path inside `image`)
terraform init -backend-config="bucket=$PROJECT-tfstate" -backend-config="prefix=llm-gateway/$ENV"

# 2a. registry, secrets (empty) and Redis first
terraform apply \
  -target=module.gateway.google_project_service.apis \
  -target=module.gateway.google_artifact_registry_repository.images \
  -target=module.gateway.google_secret_manager_secret.s \
  -target=module.gateway.google_redis_instance.cache

# 2b. secret VALUES go straight into Secret Manager, never through Terraform or its state
printf '%s' "$GROQ_API_KEY" | gcloud secrets versions add "$P-groq-api-key" --data-file=-
python -c "import secrets; print(secrets.token_urlsafe(48), end='')" \
  | gcloud secrets versions add "$P-admin-token" --data-file=-
gcloud redis instances get-auth-string "$P-redis" --region="$REGION" --format='value(authString)' \
  | tr -d '\n' | gcloud secrets versions add "$P-redis-auth" --data-file=-
uv run python scripts/make_tenant_key.py acme --rpm 600 > tenant-keys.json   # key printed once on stderr
# CD smoke tenant (scripts/smoke.py makes 1-2 tiny real calls per deploy step through it)
uv run python scripts/make_tenant_key.py smoke --rpm 30 --spec tenant-keys.json --key-file smoke.key > spec.json
mv spec.json tenant-keys.json
gcloud secrets versions add "$P-smoke-api-key" --data-file=smoke.key && rm smoke.key
gcloud secrets versions add "$P-tenant-keys" --data-file=tenant-keys.json && rm tenant-keys.json
# optional, only when enabling them (enable_anthropic / enable_hf in tfvars):
#   printf '%s' "$ANTHROPIC_API_KEY" | gcloud secrets versions add "$P-anthropic-api-key" --data-file=-
#   printf '%s' "$HF_TOKEN"          | gcloud secrets versions add "$P-hf-token" --data-file=-

# 2c. first image, pushed by digest
gcloud auth configure-docker "$REGION-docker.pkg.dev"
IMG="$REGION-docker.pkg.dev/$PROJECT/$P/gateway"
docker build -t "$IMG:bootstrap" ../../../.. && docker push "$IMG:bootstrap"
DIGEST=$(gcloud artifacts docker images describe "$IMG:bootstrap" --format='value(image_summary.digest)')

# 2d. everything else (service, worker pool, migration job, IAM)
terraform apply -var "image=$IMG@$DIGEST"
gcloud run jobs execute "$P-migrate" --region "$REGION" --wait
```

A Cloud Run revision that references a secret without a version fails to start, which is why 2b comes before
2d and why the Anthropic/HF secrets are injected only when `enable_anthropic` / `enable_hf` is set.

After this, set `GCP_PROJECT_ID` (section 1); every push to `main` deploys through `deploy.yml`.

## 3. Deploy (CD) and verify

`deploy.yml` on push to `main` (or **Run workflow**):

1. `build`: builds once, pushes `gateway:<sha>` to `llm-gateway-staging`, resolves the **digest**, and signs it
   with cosign (keyless: the certificate identity is this repo's `deploy.yml` on `main`).
2. `deploy-staging`: `cosign verify` the digest; read the smoke tenant key from `$P-smoke-api-key`;
   `gcloud run jobs update/execute` the migration job; `gcloud run deploy --no-traffic --tag canary`;
   **`scripts/smoke.py --require-functional`** against the canary URL (section 8); promote to 100%; smoke
   again; update the worker pool. Any failure: traffic goes back to the previous revision.
3. `deploy-prod` (environment `production`, waits for your approval): `cosign verify`; copies the same digest into
   `llm-gateway-prod` and checks the digest is unchanged; migrates; no-traffic canary + smoke; 10% for 5 min +
   smoke (canary and main URL); 100% + smoke; worker pool. Any failure: automatic rollback of the service
   traffic and worker image. Smoke JSON summaries are uploaded as the `smoke-staging` / `smoke-prod` artifacts.

Manual verification:

```bash
URL=$(gcloud run services describe "$P-api" --region "$REGION" --format='value(status.url)')
export SMOKE_ID_TOKEN=$(gcloud auth print-identity-token --audiences="$P-smoke")   # staging is IAM-protected
export SMOKE_API_KEY=$(gcloud secrets versions access latest --secret "$P-smoke-api-key") SMOKE_TENANT=smoke
python3 scripts/smoke.py --base-url "$URL" --expect-env "$ENV" --require-functional --json-out smoke.json
unset SMOKE_API_KEY SMOKE_ID_TOKEN
gcloud run services describe "$P-api" --region "$REGION" --format='yaml(status.traffic)'
gcloud run worker-pools describe "$P-worker" --region "$REGION"
gcloud run jobs executions list --job "$P-migrate" --region "$REGION" --limit 3
gcloud logging read "resource.type=cloud_run_revision AND resource.labels.service_name=$P-api AND severity>=WARNING" --limit 20
```

## 4. Rollback

```bash
# service: send all traffic to a known-good revision (instant, no rebuild)
gcloud run revisions list --service "$P-api" --region "$REGION"
gcloud run services update-traffic "$P-api" --region "$REGION" --to-revisions REVISION_NAME=100
# worker pool: redeploy the previous image digest
gcloud run worker-pools describe "$P-worker" --region "$REGION" --format='value(template.containers[0].image)'
gcloud run worker-pools deploy "$P-worker" --region "$REGION" --image "$IMG@sha256:PREVIOUS_DIGEST"
# infrastructure: revert the Terraform change in git, then
terraform -chdir=infra/terraform/envs/$ENV plan -var "image=$IMG@sha256:CURRENT_DIGEST"
terraform -chdir=infra/terraform/envs/$ENV apply -var "image=$IMG@sha256:CURRENT_DIGEST"
```

Migrations are additive (they add fields and sets; nothing is deleted), so an older revision runs against a
newer keyspace. If a future migration is not additive, it must say so in its docstring and the rollback
plan must be written before it ships.

## 5. Rotate secrets

Every secret is read as `latest` at instance start. After adding a version, roll a new revision so running
instances pick it up (a changed env var forces a new revision; the next `terraform apply` drops `ROTATED_AT`
again, which is harmless):

```bash
roll() {
  gcloud run services update "$P-api" --region "$REGION" --container gateway --update-env-vars "ROTATED_AT=$(date +%s)"
  gcloud run worker-pools update "$P-worker" --region "$REGION" --update-env-vars "ROTATED_AT=$(date +%s)"
}
```

* **Provider keys** (Groq/Anthropic/HF): create the new key at the provider, `printf '%s' "$NEW" | gcloud secrets
  versions add "$P-groq-api-key" --data-file=-`, `roll`, confirm traffic is healthy, revoke the old key at the
  provider, then `gcloud secrets versions disable OLD_VERSION --secret "$P-groq-api-key"`.
* **Admin token**: add a new version (command in 2b), `roll`, disable the old version.
* **Tenant keys (no downtime)**: `make_tenant_key.py acme --spec current.json > next.json` keeps the old hash and
  adds a new one; upload, `roll`; the tenant switches; then `make_tenant_key.py acme --spec next.json
  --revoke-others` (issues one more key and drops the others; hand it over), upload, `roll`. The gateway syncs
  the spec into Redis at startup and deletes hashes that left it. Get the current spec with
  `gcloud secrets versions access latest --secret "$P-tenant-keys" > current.json` (delete the file afterwards).
* **Redis AUTH** (planned blip, from the Memorystore "Manage Redis AUTH" page): Memorystore generates the string
  and keeps no overlap window, so connections fail between re-enabling AUTH and the new revision starting.
  Do it in a quiet period:
  ```bash
  gcloud redis instances update "$P-redis" --region "$REGION" --no-enable-auth   # VPC-only access meanwhile
  gcloud redis instances update "$P-redis" --region "$REGION" --enable-auth      # new string, old one invalid
  gcloud redis instances get-auth-string "$P-redis" --region "$REGION" --format='value(authString)' \
    | tr -d '\n' | gcloud secrets versions add "$P-redis-auth" --data-file=-
  roll   # the migration job reads `latest` on each execution, so it needs nothing
  ```
  Terraform keeps `auth_enabled = true`, so this does not drift. The AUTH string is also readable in Terraform
  state (a GCP-generated attribute); keep the state bucket restricted.

## 6. Kill switch (disable all LLM calls)

Two independent switches; either one stops every provider call. Interactive requests get HTTP 503
`kill_switch`, deferrable requests are refused (not queued), and the worker leaves queued jobs untouched.

```bash
# Instant, all instances, no redeploy (Redis key gw:killswitch):
ADMIN=$(gcloud secrets versions access latest --secret "$P-admin-token")
curl -sS -X PUT "$URL/admin/kill-switch" -H "X-Admin-Token: $ADMIN" -H 'Content-Type: application/json' \
  -d '{"engaged": true}'
curl -sS "$URL/admin/kill-switch" -H "X-Admin-Token: $ADMIN"           # {"engaged": true, ...}
# release: same PUT with {"engaged": false}

# Durable (survives a Redis flush; new revision), via Terraform so it is not reverted later:
terraform -chdir=infra/terraform/envs/$ENV apply -var "image=$IMG@sha256:CURRENT_DIGEST" -var kill_switch=true
# emergency path without Terraform (the next terraform apply resets it unless tfvars also says true):
gcloud run services update "$P-api" --region "$REGION" --container gateway --update-env-vars LLM_KILL_SWITCH=1
gcloud run worker-pools update "$P-worker" --region "$REGION" --update-env-vars LLM_KILL_SWITCH=1
```

Last resort if the app itself misbehaves: shift traffic to a known-good revision (section 4), or revoke the key
at the provider, which stops spend immediately and does not depend on this system working.

## 7. Common incidents

| Symptom | Likely cause | Action |
|---|---|---|
| 503 `providers_degraded` on interactive traffic | the provider breaker(s) are open | `GET /admin/providers` and `/admin/breaker/events` (admin token). With only Groq keyed there is **no failover target in prod**; check Groq status and 429s. Deferrable traffic keeps queueing. |
| Many 429s from Groq, breaker trips on `rate_limit` | provider quota | raise the Groq tier, lower tenant RPM in the tenant spec, or enable a second provider (`enable_anthropic`). |
| `skipped_budget` attempts, paid providers never called | monthly spend cap reached (key below) | intended. Raise `project_spend_cap_usd` deliberately, or wait for the next UTC month. |
| New revision never becomes ready | `/readyz` 503: Redis unreachable, or keyspace behind `REQUIRED_VERSION` | `gcloud run jobs execute "$P-migrate" --wait`; check the Direct VPC egress subnet and Redis IP; check `redis-auth` secret matches. The previous revision keeps serving. |
| All instances 503 on `/readyz` after Memorystore failover/restart | BASIC tier lost data (staging) or a failover dropped keys | re-run the migration job; tenant keys are re-synced from Secret Manager on the next instance start (`roll`). Breaker/health windows rebuild from live traffic; the spend counter for the month restarts from 0 (the paid-call JSON logs in Cloud Logging are the durable record). |
| Queue depth keeps growing | worker pool down, kill switch engaged, or all providers open | `gcloud run worker-pools describe "$P-worker"`; check `GET /admin/kill-switch`; jobs dead-letter after `max_attempts`. |
| Gateway refuses to start: `UnsafeConfiguration` | prod guard tripped (CHAOS_ENABLED, CHAOS_PROXY_URL, local provider URL, file spend ledger, plaintext Redis, short admin token) | this is the control working. Remove the offending env var; never set `CHAOS_ENABLED` in prod. |
| Breaker flaps on p95 | latency budget too tight for the provider | adjust `latency_budget_ms` in `config/gateway.prod.yaml` (a code change, through CD). |
| Alert "Fewer than 2 healthy external providers"; startup log `failover_capacity`; `/readyz` `failover.warning` | only one external provider is keyed (expected today: Groq only), or the second one's breaker is open | not an outage by itself: traffic is served, but the next provider incident is a full outage. Enable a second provider (section 11). |
| Alert "Valid-completion ratio below SLO" | a provider returns empty or truncated answers (`gateway_completions_total{validity="invalid"}` by `provider`, `reason`) | `reason="truncated"`: the class `max_tokens_cap` is too small for a reasoning model (raise it or the provider's `min_max_tokens`); `reason="empty"`: provider-side problem, move it down the preference list. |
| Alert "Tenant cost over daily budget" | one tenant's attributed spend > `tenant_daily_budget_usd` in 24 h | `GET /admin/costs`; lower that tenant's `rpm` in the tenant spec and `roll`. The project-wide hard cap still bounds total spend. |
| `scripts/smoke.py` fails in CD | the new revision is broken (or the smoke tenant key is missing) | CD already rolled back. Download the `smoke-*` artifact: each check has a PASS/FAIL line and detail. A `--require-functional` FAIL with "skipped" means the `$P-smoke-api-key` secret has no version. |

Spend counter key: `gw:spend:p3-gateway:committed:YYYYMM` (project name `p3-gateway`), reservations in
`gw:spend:p3-gateway:res`, audit list `gw:spend:p3-gateway:audit`.

## 8. Smoke tests (`scripts/smoke.py`)

One stdlib-only script, used in three places: the CI `smoke` job (compose stack with the deterministic fake
LLM), CD after the staging deploy and around the prod canary, and by hand (section 3). It prints one
`PASS` / `FAIL` / `SKIP` / `WARN` line per check, writes a JSON summary with `--json-out`, and exits 1 on any
`FAIL`. Credentials come only from env vars (`SMOKE_API_KEY` + `SMOKE_TENANT`, `SMOKE_ADMIN_TOKEN`,
`SMOKE_ID_TOKEN`); none is ever printed.

| Check | What it proves |
|---|---|
| `liveness`, `readiness` | `/healthz` 200; `/readyz` 200 with Redis reachable and the keyspace migrated |
| `failover_capacity` | `/readyz` reports healthy external providers; **WARN** (not FAIL) when fewer than 2 |
| `deploy_guards` | in staging/prod: `fake_llm=false` and `chaos_routing=false` |
| `security_headers`, `no_stack_traces` | nosniff / DENY / no-store / CSP (+ HSTS on https); 404 and malformed JSON return JSON without traces |
| `auth_rejected`, `auth_accepted` | no key 401, bad key 401, admin route without token 403; a valid key is accepted |
| `input_validation`, `oversize_rejected`, `missing_metadata_rejected` | typed 400s for bad bodies, 413 for > 64 KB, 400 `prompt_too_large`, 400 `missing_metadata`, 403 spoofed tenant |
| `chat_completion` | a real completion through the gateway, validated against an independent OpenAI-shape + gateway-metadata schema |
| `idempotent_replay` | re-send returns `X-Idempotent-Replay: true` with the same body; reused id + new body 422; with admin: exactly 1 commit |
| `metrics_breaker_gauges`, `metrics_not_public` | dev: `/metrics` exports `gateway_circuit_state` + quality/capacity series; staging/prod: `/metrics` is 404 on the public port |
| `rate_limit` | a 2-rpm tenant gets 429 `tenant_rate_limited` with `Retry-After` (dev with admin, or `SMOKE_RL_API_KEY`) |
| `kill_switch` | read-only: not engaged. `--allow-disruptive` (isolated stacks only; it is GLOBAL): engage, chat 503 `kill_switch` + `Retry-After`, release |

Never pass `--allow-disruptive` against prod: the kill switch is shared by every instance through Redis.

## 9. Alerts (Terraform: `infra/terraform/modules/gateway/monitoring.tf`)

All policies notify the email channel from `alert_email` (tfvars). Thresholds are module variables.

| Policy | Signal | Fires when | First action |
|---|---|---|---|
| `/healthz uptime check failing` (CRITICAL) | uptime check every 60 s (OIDC token when IAM-protected) | fails from 2+ regions for 5 min | section 3 verify, section 4 rollback |
| `Error budget fast burn` (CRITICAL) | PromQL, interactive 5xx / non-4xx | 1 h **and** 5 m ratio > 14.4 x (1 - SLO) | on-call checklist below |
| `Error budget slow burn` (ERROR) | same | 6 h **and** 30 m ratio > 6 x (1 - SLO) | ticket; find the degraded provider |
| `5xx ratio above threshold` (ERROR) | `run.googleapis.com/request_count` 5xx / all | > 5% for 5 min | logs of the latest revision |
| `Interactive p95 latency over SLO` (ERROR) | `gateway_client_request_seconds` histogram | p95 > 3 s for 10 min | provider p95, hedge outcomes |
| `instance saturation` (WARNING) | Cloud Run CPU/memory utilisation p99, instance count | > 85% (or > 85% of `max_instances`) for 10 min | scale limits in tfvars |
| `Provider circuit breaker open` (WARNING) | `gateway_circuit_state == 2` | 10 min | `/admin/providers`, `/admin/breaker/events` |
| `Failover rate high` (WARNING) | `gateway_failovers_total` / requests | > 20% for 10 min | preferred provider is degraded |
| `Deferrable queue depth high` (WARNING) | `gateway_queue_depth` | > 100 for 15 min | worker pool, kill switch |
| `Tenant cost over daily budget` (WARNING) | `increase(gateway_cost_usd_total[1d])` by tenant | > `tenant_daily_budget_usd` | `/admin/costs`, tenant rpm |
| `Fewer than 2 healthy external providers` (WARNING) | `gateway_external_providers_healthy` | < 2 for 15 min (fires today: Groq only) | section 11 |
| `Valid-completion ratio below SLO` (WARNING) | `gateway_completions_total` | invalid > 1% over 1 h | incident table |

## 10. On-call checklist

1. **Acknowledge** the alert; note the policy name and start time.
2. **Scope:** `curl -sS "$URL/readyz"` (redis, schema, kill switch, `failover`), then with the admin token
   `GET /admin/providers` (breaker state + window stats per provider) and `GET /admin/breaker/events`.
3. **Recent change?** `gcloud run revisions list --service "$P-api" --region "$REGION" --limit 3`. If the alert
   started within ~30 min of a rollout, roll back first (section 4), investigate second.
4. **Provider-side?** Breaker open on one provider with `rate_limit`/`server_error`: check the provider's status
   page; with a single external provider there is no failover, so consider the kill switch only if requests are
   failing *and* costing money (section 6).
5. **Run the smoke suite** against the main URL (section 3). A green run plus a firing alert points at a
   partial problem (one tenant, one class, one provider).
6. **Spend:** `GET /admin/costs`; the monthly hard cap stops paid calls by itself (`skipped_budget`).
7. **Resolve:** confirm the alert auto-closes (30 min), then write down cause, impact (minutes x requests) and
   the error budget used.

## 11. Enable a second provider (failover target)

Prod ships with **one** external provider (Groq), so the gateway warns at startup, `/readyz` carries a
`failover.warning`, and the "Fewer than 2 healthy external providers" alert fires. Nothing fakes a second one.
To add a real one:

* **Anthropic** (needs an Anthropic account and key):
  ```bash
  curl -sS https://api.anthropic.com/v1/models -H "x-api-key: $ANTHROPIC_API_KEY" -H "anthropic-version: 2023-06-01" | jq -r '.data[].id'
  printf '%s' "$ANTHROPIC_API_KEY" | gcloud secrets versions add "$P-anthropic-api-key" --data-file=-
  # tfvars: enable_anthropic = true, anthropic_model = "anthropic/<an id from the list above>"
  terraform -chdir=infra/terraform/envs/$ENV apply -var "image=$IMG@sha256:CURRENT_DIGEST"
  ```
  Terraform injects `ANTHROPIC_API_KEY` and `ANTHROPIC_MODEL`; the gateway refuses to start in staging/prod if
  the model id is still the placeholder. Check the price estimate in `config/gateway.prod.yaml` against
  <https://www.anthropic.com/pricing> before relying on the spend cap.
* **Hugging Face router** (needs credits; this account returned HTTP 402):
  `printf '%s' "$HF_TOKEN" | gcloud secrets versions add "$P-hf-token" --data-file=-`, set `enable_hf = true`,
  apply. Terraform sets `HF_ENABLED=1`.

Verify: `/readyz` shows two names in `failover.external_healthy` and no `warning`; the alert auto-closes.
