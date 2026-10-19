# SLOs and cost estimate

**Status: deploy-ready; not applied (no cloud account connected).** The targets below are initial targets for
the GCP deployment. The only measured latency/availability numbers come from the local chaos runs in
`results/summary.json` (see the README); nothing here was measured on Cloud Run.

## Service level indicators

All SLIs are computed from the gateway's own Prometheus metrics, which the Managed Service for Prometheus
sidecar ships to Cloud Monitoring (PromQL works in Metrics Explorer, alerting policies and Grafana).
"Instance" labels are added by the sidecar; sum across them.

| SLI | Definition (PromQL over the SLO window) | Notes |
|---|---|---|
| **Availability, interactive** | `sum(increase(gateway_client_requests_total{request_class=~"classify\|generate",status="200"}[30d])) / sum(increase(gateway_client_requests_total{request_class=~"classify\|generate",status!~"4.."}[30d]))` | 4xx (auth, validation, tenant rate limit, kill switch is 503 and counts as bad) are excluded as client errors. |
| **Availability, deferrable** | share of `batch` requests that end `done` (not `failed`) within 15 min of submission; today approximated by `gateway_queue_events_total{event="completed"}` vs `{event="dead_lettered"}` | a request answered `202 queued` is not yet "good"; it is good once it completes. |
| **Latency** | `histogram_quantile(0.50 \| 0.95 \| 0.99, sum by (le, request_class) (rate(gateway_client_request_seconds_bucket[5m])))` | end-to-end as seen by the gateway (includes the provider). Provider-only: `gateway_provider_latency_seconds_bucket`. |
| **Correctness: duplicate side effects** | count of `request_id`s with `gw:sidefx:* > 1` (`POST /admin/side-effects`) | must be 0. |
| **Quality: valid completions** | `sum(rate(gateway_completions_total{validity="valid"}[30d])) / sum(rate(gateway_completions_total[30d]))` | **Instrumented** (`src/gateway/quality.py`, counted once per fresh completion in `service.py`; replays are not re-counted). Valid = the response validates against the OpenAI-compatible schema + gateway block, content is non-empty, and `finish_reason != "length"`. `reason` label: `ok`, `schema`, `empty`, `truncated`. Task accuracy is out of the gateway's scope. Tests: `test_quality_sli_counts_valid_and_invalid_completions_once`, `test_classify_completion_rejects_schema_violations`. |

## Objectives (30-day rolling window)

| Objective | Target | Why this number |
|---|---|---|
| Interactive availability | **99.0%** | Prod has **one** external provider until Anthropic/HF are enabled (`config/gateway.prod.yaml`), so gateway availability is capped by Groq's. Locally, with Groq as the only independent fallback, run D measured 83.9% during a 90 s outage of both local runtimes (shared free-tier Groq key hit 429s). Raise to 99.5% once a second independent provider is enabled. |
| Deferrable completion within 15 min | **99.5%** | the queue retries with backoff for up to `max_attempts` (12). |
| Interactive latency p50 / p95 / p99 | **≤ 1.0 s / ≤ 3.0 s / ≤ 6.0 s** | Interactive p95 measured locally in run D's outage window, when Groq served most requests: 2357 ms. 6 s matches the breaker latency budget scale. Hedging needs a second provider to help. |
| Duplicate side effects | **0** | 0 of 141 duplicate submissions in the local runs. |
| Valid completions | **≥ 99%** (initial target; not yet measured on real traffic) | One invalid completion in 100 is already user-visible. Truncation is the likely cause with reasoning models (`gpt-oss` spends `max_tokens` on reasoning; `min_max_tokens: 48`), so watch `reason="truncated"` first. |

**Error budget policy.** Interactive at 99.0% leaves ~7.2 h/month. If more than 50% of the budget is gone in
the first half of the window, freeze non-urgent deploys and work the top incident cause; if it is exhausted,
only reliability changes ship until the window recovers.

**Alerts (in Terraform: `infra/terraform/modules/gateway/monitoring.tf`; table in RUNBOOK section 9).**
Multi-window burn rate on the interactive availability SLI: page when the 1 h **and** 5 m error ratios both
exceed 14.4 x (1 - 0.99) = 14.4%; ticket at 6x over 6 h **and** 30 m (the multiwindow, multi-burn-rate
recipe from the Google SRE Workbook, "Alerting on SLOs"). Plus: uptime check on `/healthz`, Cloud Run 5xx
ratio > 5%, interactive p95 > 3 s, CPU/memory p99 or instance count > 85%, breaker open 10 min, failover rate
> 20%, queue depth > 100, tenant daily cost over budget, fewer than 2 healthy external providers, and
valid-completion ratio < 99%. All validated against `hashicorp/google` 8.7.0 schemas in CI; none applied.

**Hedging.** `classify` hedges after the primary provider's recent **p95** from the shared health window,
clamped to 250-2500 ms, with a 1500 ms fallback until the window holds `min_samples` successes
(`Router.hedge_delay_s`; Dean & Barroso, "The Tail at Scale", CACM 2013). Hedging adds cost only on
hedged calls (~5% at p95) and stays capped at 30 hedges/min across instances. The effect on tail latency has
**not yet been measured** with a new chaos run; tests prove the delay selection
(`test_adaptive_hedge_fires_near_primary_p95` and neighbours in `tests/test_hardening.py`).

**Smoke suite.** `scripts/smoke.py` runs in CI against the compose stack (fake LLM) and in CD against the
staging canary, staging main URL, the prod 0% canary, the prod 10% canary and prod 100%; any failure rolls
back. It is a release gate, not an SLI: it makes 1-2 real requests per run.

## Monthly cost estimate (ESTIMATE, us-central1, list prices, before free tiers)

Prices from the GCP pricing pages on 2026-10-17: Cloud Run <https://cloud.google.com/run/pricing>,
Memorystore for Redis <https://cloud.google.com/memorystore/docs/redis/pricing>, Google Cloud Observability
(Managed Service for Prometheus, Logging) <https://cloud.google.com/stackdriver/pricing>, Secret Manager
<https://cloud.google.com/secret-manager/pricing>. A month is taken as 730 h = 2,628,000 s.

| Item | Sizing (from Terraform) | Unit price | Prod | Staging |
|---|---|---|---|---|
| Cloud Run API, instance-based billing | gateway 1 vCPU + 1 GiB, GMP collector 1 vCPU + 0.5 GiB = 2 vCPU, 1.5 GiB per instance | $0.000018 / vCPU-s, $0.000002 / GiB-s → $0.1404 per instance-hour | **$102.49** (min 1 instance, always on) + $0.14 per extra instance-hour | **~$8.42** (assumes ~60 instance-hours; scales to zero) |
| Cloud Run worker pool | 1 instance, 1 vCPU, 512 MiB, always on | Google's own pricing example for exactly this shape: $16.83/month without free tier (quoted for europe-west1; the us-central1 worker-pool rate was not captured) | **~$16.83** | **~$16.83** |
| Memorystore for Redis | 1 GiB; prod STANDARD_HA (M1), staging BASIC (M1) | Standard M1 $0.064 / GiB-h; Basic M1 $0.049 / GiB-h | **$46.72** | **$35.77** |
| Managed Service for Prometheus | ~400 series per instance (estimate), 30 s scrape → ~35 M samples per instance-month | $0.06 per million samples | **~$2.10** per instance | **< $0.50** |
| Cloud Logging | JSON request log ~0.5 KB/request | first 50 GiB/project/month free, then $0.50/GiB | $0 below ~100 M requests/month | $0 |
| Secret Manager | 4-6 secrets with one active version each | 6 active versions free per billing account, then ~$0.06/version-month; access ops 10k free then $0.03/10k | **< $1** | **< $1** |
| Artifact Registry, VPC flow logs, Memorystore egress | small | not estimated | - | - |
| **Total GCP (before free tiers)** | | | **≈ $170 / month** + $0.14 per extra API instance-hour | **≈ $62 / month** |
| Paid LLM calls | Groq (+ Anthropic/HF when enabled) | capped in code: `project_spend_cap_usd` per UTC month, enforced atomically in Redis | **≤ $25** (tfvars) | **≤ $5** |

**Cost drivers, biggest first.**
1. **Always-on API instance with instance-based billing** (min 1 instance in prod). Required for low cold-start
   latency, for the background breaker ticker, and recommended by Google for the GMP sidecar.
2. **The GMP sidecar's 1 vCPU** is ~46% of the API instance cost ($47.30 of $102.49). It is sized
   conservatively because fractional sidecar CPU limits were not verified; measure its real usage and try
   0.25-0.5 vCPU first if the bill matters.
3. **Memorystore STANDARD_HA**: the price of keeping breaker state, idempotency, the queue and the spend counter
   through a zone failure. BASIC would save ~$11/month and lose all of that on a restart.
4. **Autoscaling**: every extra API instance-hour adds ~$0.14; `max_instances` (10 in prod) bounds it at
   ~$1.40/hour above baseline.
5. **LLM spend** is bounded by the cap, not by traffic.

Committed use discounts (Cloud Run, Memorystore) and the monthly free tiers would lower these numbers; they are
deliberately left out so the estimate errs high.
