# Demo recording

`demo.gif` is a **real session**, recorded on Windows 11 on 2026-10-18 against the local dev stack:
Redis 7 (docker, `:16380`), the gateway (`python -m gateway.app`, `:8103`, `CHAOS_ENABLED=1`,
`CHAOS_PROXY_URL=http://127.0.0.1:8113`), the chaos proxy (`python -m gateway.chaos`, `:8113`),
host Ollama with `llama3.1:8b` on the GPU, the `ollama-cpu` container with `qwen2.5:0.5b`, and
Groq `openai/gpt-oss-20b`. It plays at real speed, 35.6 s, except that agg caps idle gaps at 3 s
(`--idle-time-limit 3`), which trimmed about 4 s of waiting from the 34.4 s recording plus the 5 s last frame.

## What is real, what is not

* **Real:** every line printed by `uv run python assets/demo/demo.py`. It sends `classify`
  requests to the running gateway, tells the chaos proxy to answer every `ollama` call with
  HTTP 503, clears the fault, and re-sends an already-answered `request_id`. Providers, latencies,
  answers, per-attempt outcomes (`server_error`, `skipped_open`, `cancelled`, probe flags), the
  replay header and the commit counter all come from the gateway's responses. The `BREAKER` lines
  are read from the gateway's own transition log (`GET /admin/breaker/events`), not inferred.
* **Synthetic:** the amber `#` comment lines and the typing pace of the one command, both printed
  by `record.py`.
* **Nothing cached:** every request in the session was a live model call. The 17 Groq answers cost
  real (estimated list-price) money and are in the shared spend ledger; all four recording
  attempts together were 87 Groq calls, about $0.0024.
* **Not shown:** tokens and the tenant API key (read from the git-ignored `.env`, never printed).

## What the session shows, and one surprise

1. Healthy: `ollama` (GPU) answers. Some calls take over 2.5 s, so the `classify` hedge fires on
   `ollama_cpu` and loses.
2. Fault injected: `ollama` returns 503; the breaker trips on `error_rate` within about 3 s;
   later requests skip `ollama` without calling it (`ollama open: skipped`).
3. Fault cleared: the breaker stays open for its 10 s cooldown, goes `half_open`, three probes
   succeed, and it closes. Normal traffic returns to `ollama`.
4. Idempotency: three re-sends of an answered `request_id` all get
   `X-Idempotent-Replay: true` and the same answer; `/admin/side-effects` reports 1 commit.

**Surprise:** in this session the same-host CPU runtime (`ollama_cpu`) lost *every* hedge race:
its answers through the gateway took longer than the 2.5 s hedge delay plus Groq's ~0.4 s, so
traffic during the outage was served by Groq, the independent external provider. Single calls sent straight to
the container, and through the chaos proxy, right after the session took 0.1-0.6 s (one cold call took
13.7 s), so the slowdown was somewhere on the gateway → LiteLLM → chaos proxy → container path under load.

**Found afterwards (2026-10-19, `scripts/hedge_probe.py`):** the chaos proxy did not pass the gateway's
cancellation on. When the router cancels a hedge loser it closes its connection to the proxy, but the proxy
kept waiting on the container, so Ollama kept generating the abandoned answer and the next call queued
behind it. That call was then slower than the hedge delay, lost, was cancelled, and left more abandoned work:
a self-reinforcing loop on a CPU runtime. Measured with the gateway's own provider code, 9 calls per cell,
each sent right after a cancelled 256-token call:

| Path | p50 | p95 | calls over 2.5 s |
|---|---|---|---|
| direct to the container | 251 ms | 899 ms | 0 of 9 |
| through the chaos proxy, **before** the fix | 1663 ms | 7055 ms | 4 of 9 |
| through the chaos proxy, **after** the fix | 266 ms | 436 ms | 0 of 9 |

Raw data: `results/ollama_cpu_hedge_probe_before_fix.json`, `results/ollama_cpu_hedge_probe_after_fix.json`.
The proxy now cancels its upstream call when the gateway disconnects (`chaos_upstream_cancelled_total`;
`tests/test_components.py::test_chaos_proxy_cancels_upstream_when_the_gateway_disconnects`). This is a
dev-tool artefact: the chaos proxy never runs in prod, and direct calls were unaffected. The demo itself was
not re-recorded. Three concurrent calls on the CPU runtime still reach ~2.3 s at p95 on both paths, so a
fixed 2.5 s hedge delay was borderline for it anyway; `classify` now hedges after the primary's p95.

## Re-record

Bring the stack up (see the main README's Quick start), make sure both Ollama models are loaded,
then from the repo root (`resvg`, `agg` 1.5.0 and JetBrains Mono needed; the font is unpacked from the
brand build cache):

```sh
uv run python assets/demo/record.py assets/demo/demo.cast
mkdir -p assets/demo/.work/fonts && unzip -o -j assets/brand/tools/.cache/JetBrainsMono-2.304.zip \
  'fonts/ttf/JetBrainsMono-Regular.ttf' 'fonts/ttf/JetBrainsMono-Bold.ttf' -d assets/demo/.work/fonts
agg --cols 110 --rows 30 --font-dir assets/demo/.work/fonts --font-family "JetBrains Mono" \
    --theme 0c1417,eef4f1,0c1417,f47067,34d399,fbbf24,58a6ff,d2a8ff,39c5cf,c9d1d9,6e7681,ff7b72,56d364,f5c451,79c0ff,e2b6ff,56d4dd,eef4f1 \
    --font-size 18 --idle-time-limit 3 --last-frame-duration 5 assets/demo/demo.cast assets/demo/.work/raw.gif
uv run --no-project --with pillow python assets/demo/frame.py assets/demo/.work/raw.gif assets/demo/demo.gif "llmfailover  -  real session"
```

`record.py` exists because asciinema has no Windows build: it runs the command through a plain
subprocess (no pty), timestamps each stdout chunk as it arrives and writes asciicast v2. Flush
the dev Redis between takes (`docker exec p3-gateway-redis-1 redis-cli FLUSHDB`, then
`uv run python -m gateway.migrate`) so each take starts with closed breakers. The theme is the
brand palette (Ink background, Snow text) with GitHub-dark ANSI colours; `frame.py` adds the
window chrome.
