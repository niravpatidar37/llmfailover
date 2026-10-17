"""Chaos load generator: steady traffic -> provider outage -> recovery, measured from the client side.

Every number in results/ comes from this script talking to a running gateway + chaos proxy. Nothing is
simulated on the client: availability is "the client got a usable answer" (HTTP 200, or a 202 deferral that
later completed), latency is wall-clock at the client, and duplicate side effects are read back from the
gateway's per-request_id commit counters.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import random
import sys
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

import httpx
from dotenv import dotenv_values

ROOT = Path(__file__).resolve().parents[1]
ENV = dotenv_values(ROOT / ".env")
PROMPTS = [
    json.loads(x)
    for x in (ROOT / "scripts/prompts/sst2_validation_100.jsonl").read_text("utf-8").splitlines()
]

# tenant -> [(feature, request_class, weight)]
TENANTS: dict[str, list[tuple[str, str, float]]] = {
    "acme": [("support-triage", "classify", 0.30), ("email-draft", "generate", 0.05)],
    "globex": [
        ("review-sentiment", "classify", 0.25),
        ("nightly-summaries", "batch", 0.10),
        ("reply-suggest", "generate", 0.05),
    ],
    "initech": [("ticket-router", "classify", 0.15), ("report-batch", "batch", 0.10)],
}
FIRST_PREF = {"classify": "ollama", "generate": "groq", "batch": "ollama"}


@dataclass
class Rec:
    rid: str
    tenant: str
    feature: str
    cls: str
    t_send: float
    phase: str
    status: int = 0
    latency_ms: float = 0.0
    provider: str | None = None
    failovers: int = 0
    attempts: list[dict[str, Any]] = field(default_factory=list)
    deferred: bool = False
    deferred_done_s: float | None = None
    ok: bool = False
    error: str | None = None
    duplicate_of: str | None = None
    replay: bool = False
    replay_matches: bool | None = None


def build_body(tenant: str, feature: str, cls: str, rid: str, rng: random.Random) -> dict[str, Any]:
    s = rng.choice(PROMPTS)["sentence"]
    if cls == "classify":
        content, mt = f"Sentiment of this movie review, positive or negative? One word.\nReview: {s}", 4
    elif cls == "generate":
        content, mt = f"Write a one-sentence reply to this movie review: {s}", 24
    else:
        content, mt = f"Summarize in at most six words: {s}", 12
    return {
        "model": "auto",
        "messages": [{"role": "user", "content": content}],
        "max_tokens": mt,
        "metadata": {"tenant": tenant, "feature": feature, "request_id": rid, "request_class": cls},
    }


def pct(xs: list[float], q: float) -> float | None:
    if not xs:
        return None
    xs = sorted(xs)
    return round(xs[min(len(xs) - 1, max(0, round(q * (len(xs) - 1))))], 1)


class LoadGen:
    def __init__(self, args: argparse.Namespace) -> None:
        self.a = args
        self.gw = args.gateway
        self.admin = {"X-Admin-Token": ENV.get("GATEWAY_ADMIN_TOKEN") or ""}
        self.chaos = {"X-Chaos-Token": ENV.get("CHAOS_ADMIN_TOKEN") or ""}
        self.keys: dict[str, str] = {}
        self.recs: list[Rec] = []
        self.dups: list[Rec] = []
        self.rng = random.Random(args.seed)
        self.t0 = 0.0
        self.marks: dict[str, float] = {}
        self.client = httpx.AsyncClient(timeout=httpx.Timeout(90.0), limits=httpx.Limits(max_connections=200))
        self.tasks: list[asyncio.Task[None]] = []

    def phase_of(self, t: float) -> str:
        rel = t - self.t0
        a = self.a
        if rel < a.warmup:
            return "warmup"
        if rel < a.warmup + a.baseline:
            return "baseline"
        if rel < a.warmup + a.baseline + a.outage:
            return "outage"
        return "recovery"

    async def setup(self) -> None:
        for _ in range(60):  # the gateway imports LiteLLM at startup; wait for readiness
            try:
                if (await self.client.get(f"{self.gw}/healthz")).status_code == 200:
                    break
            except httpx.HTTPError:
                pass
            await asyncio.sleep(1)
        for t in TENANTS:
            r = await self.client.post(
                f"{self.gw}/admin/tenants", json={"tenant": t, "rpm": 6000}, headers=self.admin
            )
            r.raise_for_status()
            self.keys[t] = r.json()["api_key"]

    async def send(self, rec: Rec, body: dict[str, Any]) -> dict[str, Any] | None:
        h = {"Authorization": f"Bearer {self.keys[rec.tenant]}"}
        t = time.perf_counter()
        try:
            r = await self.client.post(f"{self.gw}/v1/chat/completions", json=body, headers=h)
        except httpx.HTTPError as exc:
            rec.error, rec.latency_ms = type(exc).__name__, (time.perf_counter() - t) * 1000
            return None
        rec.latency_ms = (time.perf_counter() - t) * 1000
        rec.status = r.status_code
        j = r.json()
        rec.replay = r.headers.get("X-Idempotent-Replay") == "true"
        if r.status_code == 200:
            g = j["gateway"]
            rec.provider, rec.failovers, rec.attempts, rec.ok = (
                g["provider"],
                g["failovers"],
                g["attempts"],
                True,
            )
        elif r.status_code == 202:
            rec.deferred = True
            await self.poll(rec, h)
        else:
            rec.error = j.get("error", {}).get("code")
            rec.attempts = j.get("error", {}).get("attempts", [])
        return j if r.status_code == 200 else None

    async def poll(self, rec: Rec, h: dict[str, str]) -> None:
        start = time.perf_counter()
        while time.perf_counter() - start < self.a.defer_deadline:
            await asyncio.sleep(1.0)
            r = await self.client.get(f"{self.gw}/v1/requests/{rec.rid}", headers=h)
            if r.status_code == 200:
                g = r.json()["gateway"]
                rec.ok, rec.provider, rec.failovers = True, g["provider"], g["failovers"]
                rec.deferred_done_s = round(time.perf_counter() - start, 2)
                return
            if r.status_code not in (202,):
                rec.error = f"deferred_{r.status_code}"
                return
        rec.error = "deferred_timeout"

    async def one(self, tenant: str, feature: str, cls: str) -> None:
        rid = f"lg-{uuid.uuid4().hex[:16]}"
        body = build_body(tenant, feature, cls, rid, self.rng)
        rec = Rec(rid, tenant, feature, cls, time.time(), self.phase_of(time.time()))
        self.recs.append(rec)
        concurrent_dup = self.rng.random() < self.a.concurrent_dup_ratio
        if concurrent_dup:  # same request_id fired in parallel: must not execute twice
            dup = Rec(rid, tenant, feature, cls, time.time(), rec.phase, duplicate_of=rid)
            self.dups.append(dup)
            res = await asyncio.gather(self.send(rec, body), self.send(dup, body))
            if res[0] is not None and res[1] is not None:
                dup.replay_matches = res[0]["choices"] == res[1]["choices"]
            return
        first = await self.send(rec, body)
        if first is not None and self.rng.random() < self.a.dup_ratio:  # client retry after success
            await asyncio.sleep(self.rng.uniform(0.0, 2.0))
            dup = Rec(rid, tenant, feature, cls, time.time(), rec.phase, duplicate_of=rid)
            self.dups.append(dup)
            again = await self.send(dup, body)
            dup.replay_matches = again is not None and again["choices"] == first["choices"]

    async def chaos_at(self, when: float, fn: str) -> None:
        await asyncio.sleep(max(0.0, when - (time.time() - self.t0)))
        for prov in self.a.provider.split(","):
            url = f"{self.a.chaos}/admin/faults/{prov}"
            if fn == "inject":
                fault = {"error_rate": self.a.error_rate, "latency_ms": self.a.latency_ms, "status_code": 503}
                r = await self.client.put(url, json=fault, headers=self.chaos)
            else:
                r = await self.client.delete(url, headers=self.chaos)
            r.raise_for_status()
        self.marks["outage_start" if fn == "inject" else "outage_end"] = time.time()
        print(f"[{time.time() - self.t0:6.1f}s] chaos {fn} {self.a.provider}", flush=True)

    async def screenshot_at(self, when: float, name: str) -> None:
        await asyncio.sleep(max(0.0, when - (time.time() - self.t0)))
        out = ROOT / "docs" / f"{name}.png"
        proc = await asyncio.create_subprocess_exec(
            sys.executable,
            str(ROOT / "scripts/grafana_screenshot.py"),
            "--out",
            str(out),
            "--range",
            "now-8m",
            "--wait",
            "6",
        )
        await proc.wait()
        self.marks[f"screenshot_{name}"] = time.time()
        print(f"[{time.time() - self.t0:6.1f}s] screenshot {name} rc={proc.returncode}", flush=True)

    async def run(self) -> dict[str, Any]:
        a = self.a
        await self.setup()
        ledger_before = ledger_total()
        self.t0 = time.time()
        self.marks["start"] = self.t0
        total = a.warmup + a.baseline + a.outage + a.recovery
        bg = [
            asyncio.create_task(self.chaos_at(a.warmup + a.baseline, "inject")),
            asyncio.create_task(self.chaos_at(a.warmup + a.baseline + a.outage, "clear")),
        ]
        for spec in a.screenshot:
            name, at = spec.split("@")
            bg.append(asyncio.create_task(self.screenshot_at(float(at), name)))
        mix = [(t, f, c, w) for t, items in TENANTS.items() for f, c, w in items]
        weights = [w for *_, w in mix]
        next_t = time.time()
        while time.time() - self.t0 < total:
            next_t += self.rng.expovariate(a.rate)
            await asyncio.sleep(max(0.0, next_t - time.time()))
            t, f, c, _ = self.rng.choices(mix, weights)[0]
            self.tasks.append(asyncio.create_task(self.one(t, f, c)))
            n = len(self.recs)
            if n and n % 50 == 0:
                done = [r for r in self.recs if r.status]
                okn = sum(r.ok for r in done)
                print(f"[{time.time() - self.t0:6.1f}s] sent={n} ok={okn}/{len(done)}", flush=True)
        self.marks["traffic_end"] = time.time()
        await asyncio.gather(*self.tasks)
        await asyncio.gather(*bg)
        self.marks["end"] = time.time()
        return await self.summarize(ledger_before)

    async def summarize(self, ledger_before: float) -> dict[str, Any]:
        ev = (
            await self.client.get(
                f"{self.gw}/admin/breaker/events", params={"since": self.t0}, headers=self.admin
            )
        ).json()["events"]
        sidefx: dict[str, int] = {}
        for t in TENANTS:
            rids = [r.rid for r in self.recs if r.tenant == t]
            for i in range(0, len(rids), 1000):
                r = await self.client.post(
                    f"{self.gw}/admin/side-effects",
                    json={"tenant": t, "request_ids": rids[i : i + 1000]},
                    headers=self.admin,
                )
                sidefx.update({f"{t}:{k}": v for k, v in r.json()["counts"].items()})
        costs = (await self.client.get(f"{self.gw}/admin/costs", headers=self.admin)).json()
        os_, oe = self.marks["outage_start"], self.marks["outage_end"]

        def phase_stats(recs: list[Rec]) -> dict[str, Any]:
            n = len(recs)
            ok = sum(r.ok for r in recs)
            inter = [r for r in recs if r.cls != "batch"]
            lat = [r.latency_ms for r in inter if r.ok]
            served: dict[str, int] = {}
            for r in recs:
                if r.ok and r.provider:
                    served[r.provider] = served.get(r.provider, 0) + 1
            errs: dict[str, int] = {}
            for r in recs:
                if not r.ok:
                    errs[r.error or str(r.status)] = errs.get(r.error or str(r.status), 0) + 1
            return {
                "requests": n,
                "succeeded": ok,
                "availability_pct": round(100 * ok / n, 3) if n else None,
                "interactive_requests": len(inter),
                "interactive_succeeded": sum(r.ok for r in inter),
                "interactive_availability_pct": round(100 * sum(r.ok for r in inter) / len(inter), 3)
                if inter
                else None,
                "deferred_202": sum(r.deferred for r in recs),
                "deferred_completed": sum(r.deferred and r.ok for r in recs),
                "deferred_completion_s_p50": pct([r.deferred_done_s for r in recs if r.deferred_done_s], 0.5),
                "deferred_completion_s_max": max([r.deferred_done_s or 0 for r in recs], default=None),
                "latency_ms_interactive_p50": pct(lat, 0.5),
                "latency_ms_interactive_p95": pct(lat, 0.95),
                "latency_ms_interactive_p99": pct(lat, 0.99),
                "served_by": served,
                "failures": errs,
                "failover_events": sum(r.failovers for r in recs if r.ok),
                "served_off_first_preference": sum(
                    1 for r in recs if r.ok and r.provider != FIRST_PREF[r.cls]
                ),
                "hedged_attempts": sum(1 for r in recs for x in r.attempts if x.get("hedge")),
            }

        phases = {
            p: phase_stats([r for r in self.recs if r.phase == p])
            for p in ("warmup", "baseline", "outage", "recovery")
        }
        rel = [{**e, "t_rel_outage_start_s": round(e["ts"] - os_, 2)} for e in ev]
        prov_ev = [e for e in rel if e["provider"] == self.a.provider.split(",")[0]]
        first_open = next((e for e in prov_ev if e["to"] == "open" and e["ts"] >= os_), None)
        closed_after = next((e for e in prov_ev if e["to"] == "closed" and e["ts"] >= oe), None)
        dup_side_effects = sum(max(0, v - 1) for v in sidefx.values())
        result = {
            "config": {k: v for k, v in vars(self.a).items() if k not in ("gateway", "chaos")},
            "marks_unix": self.marks,
            "outage_window_s": round(oe - os_, 2),
            "phases": phases,
            "headline": {
                "outage_client_availability_pct": phases["outage"]["availability_pct"],
                "outage_interactive_availability_pct": phases["outage"]["interactive_availability_pct"],
                "overall_availability_pct": round(100 * sum(r.ok for r in self.recs) / len(self.recs), 3),
                "requests_total": len(self.recs),
                "failover_events_total": sum(p["failover_events"] for p in phases.values()),
                "breaker_tripped_s_after_outage_start": round(first_open["ts"] - os_, 2)
                if first_open
                else None,
                "breaker_trip_reason": first_open["reason"] if first_open else None,
                "breaker_closed_s_after_outage_end": round(closed_after["ts"] - oe, 2)
                if closed_after
                else None,
                "p95_ms_before": phases["baseline"]["latency_ms_interactive_p95"],
                "p95_ms_during": phases["outage"]["latency_ms_interactive_p95"],
                "p95_ms_after": phases["recovery"]["latency_ms_interactive_p95"],
                "duplicate_submissions": len(self.dups),
                "duplicate_replays_identical": sum(1 for d in self.dups if d.replay_matches),
                "duplicate_side_effects": dup_side_effects,
                "request_ids_with_commit_count_gt_1": sum(1 for v in sidefx.values() if v > 1),
                "spend_usd_this_run_from_ledger": round(ledger_total() - ledger_before, 6),
            },
            "breaker_events": rel,
            "costs_cumulative": costs,
            "requests": [asdict(r) for r in self.recs],
            "duplicates": [asdict(d) for d in self.dups],
        }
        await self.client.aclose()
        return result


def ledger_total() -> float:
    sys.path.insert(0, str(ROOT / "src"))
    from gateway.config import load_secrets
    from gateway.spend import SpendLedger

    load_secrets()
    import os

    led = SpendLedger(
        os.environ.get("LLM_SPEND_LEDGER", "F:/.llm-spend/ledger.jsonl"), "p3-gateway", 1.0, 25.0
    )
    return led.totals()[1]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--gateway", default="http://127.0.0.1:8103")
    ap.add_argument("--chaos", default="http://127.0.0.1:8113")
    ap.add_argument("--provider", default="ollama")
    ap.add_argument("--rate", type=float, default=1.0, help="mean requests/s (Poisson)")
    ap.add_argument("--warmup", type=float, default=20)
    ap.add_argument("--baseline", type=float, default=90)
    ap.add_argument("--outage", type=float, default=120)
    ap.add_argument("--recovery", type=float, default=120)
    ap.add_argument("--error-rate", type=float, default=1.0)
    ap.add_argument("--latency-ms", type=int, default=0)
    ap.add_argument("--dup-ratio", type=float, default=0.10)
    ap.add_argument("--concurrent-dup-ratio", type=float, default=0.03)
    ap.add_argument("--defer-deadline", type=float, default=240)
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--screenshot", action="append", default=[], help="name@seconds_from_start")
    ap.add_argument("--out", default=str(ROOT / "results" / "chaos_run.json"))
    args = ap.parse_args()
    res = asyncio.run(LoadGen(args).run())
    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    Path(args.out).write_text(json.dumps(res, indent=1), encoding="utf-8")
    print(json.dumps(res["headline"], indent=1))
    print(
        json.dumps(
            {
                p: {
                    k: v
                    for k, v in s.items()
                    if k
                    in ("requests", "availability_pct", "served_by", "latency_ms_interactive_p95", "failures")
                }
                for p, s in res["phases"].items()
            },
            indent=1,
        )
    )


if __name__ == "__main__":
    main()
