#!/usr/bin/env bash
# Smoke test a deployed gateway URL: /healthz must be 200 and /readyz must report ready.
#   scripts/smoke_deploy.sh https://canary---llm-gateway-staging-api-xyz-uc.a.run.app
# Optional: SMOKE_ID_TOKEN (identity token for IAM-protected services), SMOKE_TRIES (default 10).
set -euo pipefail
url="${1:?usage: smoke_deploy.sh URL}"
tries="${SMOKE_TRIES:-10}"
auth=()
if [ -n "${SMOKE_ID_TOKEN:-}" ]; then
  auth=(-H "X-Serverless-Authorization: Bearer ${SMOKE_ID_TOKEN}")
fi
check() {
  local path="$1" want="$2" body code
  body="$(mktemp)"
  code="$(curl -sS -o "$body" -w '%{http_code}' --max-time 10 "${auth[@]}" "${url}${path}" || echo 000)"
  if [ "$code" = "200" ] && grep -q "$want" "$body"; then
    echo "ok   ${path} -> ${code}"
    rm -f "$body"
    return 0
  fi
  echo "FAIL ${path} -> ${code}: $(head -c 300 "$body")"
  rm -f "$body"
  return 1
}
for i in $(seq 1 "$tries"); do
  if check /healthz '"status":"ok"' && check /readyz '"status":"ready"'; then
    echo "smoke passed for ${url}"
    exit 0
  fi
  echo "attempt ${i}/${tries} failed; retrying in 6 s"
  sleep 6
done
echo "smoke FAILED for ${url}"
exit 1
