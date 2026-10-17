# Runbook: self-healing LLM gateway on Google Cloud Run

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
export REPO=niravpatidar37/self-healing-llm-gateway
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

1. `build`: builds once, pushes `gateway:<sha>` to `llm-gateway-staging`, resolves the **digest**.
2. `deploy-staging`: `gcloud run jobs update/execute` the migration job; `gcloud run deploy --no-traffic --tag canary`;
   `scripts/smoke_deploy.sh` against the canary URL (`/healthz` 200 and `/readyz` "ready"); promote to 100%;
   smoke again; update the worker pool. Any failure: traffic goes back to the previous revision.
3. `deploy-prod` (environment `production`, waits for your approval): copies the same digest into
   `llm-gateway-prod` and checks the digest is unchanged; migrates; no-traffic canary + smoke; 10% for 5 min +
   smoke; 100% + smoke; worker pool. Any failure: automatic rollback of the service traffic and worker image.

Manual verification:

```bash
URL=$(gcloud run services describe "$P-api" --region "$REGION" --format='value(status.url)')
TOKEN=$(gcloud auth print-identity-token --audiences="$P-smoke")   # staging is IAM-protected
SMOKE_ID_TOKEN=$TOKEN bash scripts/smoke_deploy.sh "$URL"
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

Spend counter key: `gw:spend:p3-gateway:committed:YYYYMM` (project name `p3-gateway`), reservations in
`gw:spend:p3-gateway:res`, audit list `gw:spend:p3-gateway:audit`.
