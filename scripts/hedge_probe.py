"""Why did ``ollama_cpu`` lose every hedge race in the demo? A short, reproducible latency probe.

It calls the CPU runtime (``ollama_cpu`` from config/gateway.yaml: qwen2.5:0.5b in the ``ollama-cpu``
container) through the gateway's own provider code (``LiteLLMCaller``), on two paths:

* ``direct``: gateway process -> container (127.0.0.1:11435)
* ``proxy``:  gateway process -> chaos proxy (127.0.0.1:8113/ollama_cpu) -> container, the path the demo used

and three scenarios:

* ``sequential``: one short classify-sized call at a time (max_tokens 16), the healthy baseline;
* ``after_cancelled_loser``: a long call (max_tokens 256) is started and cancelled after 200 ms, exactly like
  the router cancels a hedge loser, and a short call is sent right after it. If the runtime keeps working on
  the abandoned call, the short call queues behind it;
* ``concurrent_3``: three short calls at once (outage traffic + hedges overlapping on one CPU runtime).

    CHAOS_ENABLED=1 OLLAMA_CPU_URL=http://127.0.0.1:11435 uv run python -m gateway.chaos &  # proxy path
    uv run python scripts/hedge_probe.py --out results/ollama_cpu_hedge_probe_after_fix.json

$0: local runtime only. No Groq calls.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import os
import statistics
import time
from pathlib import Path
from typing import Any

import httpx

from gateway.config import ProviderConfig, load_config
from gateway.errors import ProviderError
from gateway.providers import LiteLLMCaller

ROOT = Path(__file__).resolve().parents[1]
SHORT = [{"role": "user", "content": "Classify the sentiment as positive or negative: 'a delightful film'."}]
LONG = [{"role": "user", "content": "Write a detailed, multi-paragraph review of a film you liked."}]


def pct(xs: list[float], q: float) -> float:
    s = sorted(xs)
    return round(s[min(len(s) - 1, max(0, round(q * (len(s) - 1))))], 1)


def summary(xs: list[float]) -> dict[str, Any]:
    return {
        "n": len(xs),
        "p50_ms": pct(xs, 0.5),
        "p95_ms": pct(xs, 0.95),
        "mean_ms": round(statistics.fmean(xs), 1),
        "max_ms": round(max(xs), 1),
        "over_2500ms": sum(1 for x in xs if x > 2500),
        "raw_ms": [round(x, 1) for x in xs],
    }


async def timed(call: LiteLLMCaller, p: ProviderConfig, msgs: list[dict[str, str]], mt: int) -> float:
    t0 = time.perf_counter()
    try:
        await call(p, msgs, mt, 0.0)
    except ProviderError as exc:
        print("  error:", exc)
    return (time.perf_counter() - t0) * 1000


async def ps() -> list[dict[str, Any]]:
    async with httpx.AsyncClient(timeout=5) as c:
        r = await c.get("http://127.0.0.1:11435/api/ps")
        return [{"name": m.get("name"), "size_vram": m.get("size_vram")} for m in r.json().get("models", [])]


async def run_path(name: str, p: ProviderConfig, n: int) -> dict[str, Any]:
    call = LiteLLMCaller()
    await timed(call, p, SHORT, 16)  # warm (model load is excluded on purpose; it is reported separately)
    seq = [await timed(call, p, SHORT, 16) for _ in range(n)]
    after: list[float] = []
    for _ in range(n):
        loser = asyncio.create_task(call(p, LONG, 256, 0.0))
        await asyncio.sleep(0.2)
        loser.cancel()
        with contextlib.suppress(asyncio.CancelledError, ProviderError):
            await loser
        after.append(await timed(call, p, SHORT, 16))
        await asyncio.sleep(3.0)  # let any abandoned generation drain before the next round
    conc: list[float] = []
    for _ in range(max(1, n // 3)):
        conc += await asyncio.gather(*(timed(call, p, SHORT, 16) for _ in range(3)))
    out = {
        "sequential": summary(seq),
        "after_cancelled_loser": summary(after),
        "concurrent_3": summary(conc),
    }
    print(
        f"{name:7} seq p50 {out['sequential']['p50_ms']} ms | after-cancel p50 "
        f"{out['after_cancelled_loser']['p50_ms']} ms | concurrent p50 {out['concurrent_3']['p50_ms']} ms"
    )
    return out


async def main() -> tuple[str, dict[str, Any]]:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=str(ROOT / "results" / "ollama_cpu_hedge_probe.json"))
    ap.add_argument("-n", type=int, default=9)
    ap.add_argument("--proxy", default="http://127.0.0.1:8113")
    ap.add_argument("--skip-proxy", action="store_true")
    a = ap.parse_args()
    os.environ.pop("CHAOS_ENABLED", None)  # api_base below is set explicitly per path
    base = load_config(ROOT / "config" / "gateway.yaml").providers["ollama_cpu"]
    direct = base.model_copy(update={"api_base_default": "http://127.0.0.1:11435", "api_base_env": None})
    res: dict[str, Any] = {
        "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "provider": "ollama_cpu",
        "model": base.litellm_model,
        "runtime": "ollama/ollama:0.40.5 container, CPU only, OLLAMA_NUM_PARALLEL=2",
        "loaded_models_before": await ps(),
        "paths": {},
    }
    res["paths"]["direct"] = await run_path("direct", direct, a.n)
    if not a.skip_proxy:
        proxied = base.model_copy(update={"api_base_default": f"{a.proxy}/ollama_cpu", "api_base_env": None})
        res["paths"]["proxy"] = await run_path("proxy", proxied, a.n)
    return a.out, res


if __name__ == "__main__":
    out, res = asyncio.run(main())
    Path(out).write_text(json.dumps(res, indent=2) + "\n", encoding="utf-8", newline="\n")
    print("wrote", out)
