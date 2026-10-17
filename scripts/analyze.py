"""Aggregate raw load-generator results into results/summary.json and a markdown table (README source)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
RUNS = {
    "outage": ROOT / "results/chaos_run.json",
    "brownout": ROOT / "results/brownout_run.json",
    "total_outage": ROOT / "results/total_outage_run.json",
    "local_host_outage": ROOT / "results/local_host_outage_run.json",
}


def pct(xs: list[float], q: float) -> float | None:
    if not xs:
        return None
    xs = sorted(xs)
    return round(xs[min(len(xs) - 1, max(0, round(q * (len(xs) - 1))))], 1)


def window(reqs: list[dict[str, Any]], t0: float, t1: float) -> dict[str, Any]:
    rs = [r for r in reqs if t0 <= r["t_send"] < t1]
    inter = [r for r in rs if r["cls"] != "batch"]
    ok_lat = [r["latency_ms"] for r in inter if r["ok"]]
    fail_lat = [r["latency_ms"] for r in inter if not r["ok"]]
    return {
        "requests": len(rs),
        "availability_pct": round(100 * sum(r["ok"] for r in rs) / len(rs), 2) if rs else None,
        "interactive_availability_pct": round(100 * sum(r["ok"] for r in inter) / len(inter), 2)
        if inter
        else None,
        "interactive_p50_ms": pct(ok_lat, 0.5),
        "interactive_p95_ms": pct(ok_lat, 0.95),
        "failed_interactive_p95_ms": pct(fail_lat, 0.95),
        "deferred_202": sum(r["deferred"] for r in rs),
        "deferred_completed": sum(r["deferred"] and r["ok"] for r in rs),
        "deferred_completion_s_max": max(
            [r["deferred_done_s"] or 0 for r in rs if r["deferred"]], default=None
        ),
    }


def analyze(name: str, d: dict[str, Any]) -> dict[str, Any]:
    m = d["marks_unix"]
    reqs = d["requests"]
    prov = d["config"]["provider"].split(",")[0]
    closed = next(
        (
            e["ts"]
            for e in d["breaker_events"]
            if e["provider"] == prov and e["to"] == "closed" and e["ts"] >= m["outage_end"]
        ),
        None,
    )
    end = m["traffic_end"]
    # Breaker headline from raw events: first trip after fault start, first close after fault end.
    trip = next(
        (
            e
            for e in d["breaker_events"]
            if e["provider"] == prov and e["to"] == "open" and e["ts"] >= m["outage_start"]
        ),
        None,
    )
    headline = dict(d["headline"])
    headline["breaker_tripped_s_after_outage_start"] = (
        round(trip["ts"] - m["outage_start"], 2) if trip else None
    )
    headline["breaker_trip_reason"] = trip["reason"] if trip else None
    headline["breaker_closed_s_after_outage_end"] = round(closed - m["outage_end"], 2) if closed else None
    pre = [e for e in d["breaker_events"] if e["ts"] < m["outage_start"] and e["to"] == "open"]
    headline["breaker_trips_before_fault"] = [
        {"provider": e["provider"], "reason": e["reason"], "t": e["t_rel_outage_start_s"]} for e in pre
    ]
    out = {
        "scenario": name,
        "fault": {
            "providers": d["config"]["provider"],
            "error_rate": d["config"]["error_rate"],
            "latency_ms": d["config"]["latency_ms"],
            "duration_s": d["outage_window_s"],
        },
        "rate_rps": d["config"]["rate"],
        "headline": headline,
        "windows": {
            "baseline": window(reqs, m["start"] + d["config"]["warmup"], m["outage_start"]),
            "outage": window(reqs, m["outage_start"], m["outage_end"]),
            "recovery_breaker_still_open": window(reqs, m["outage_end"], closed or end),
            "recovery_after_breaker_closed": window(reqs, closed or end, end),
        },
        "breaker_timeline_s_from_fault_start": [
            {
                "provider": e["provider"],
                "t": e["t_rel_outage_start_s"],
                "transition": f"{e['from']}->{e['to']}",
                "reason": e["reason"],
            }
            for e in d["breaker_events"]
        ],
        "phases": {
            p: {
                k: v
                for k, v in s.items()
                if k in ("served_by", "failover_events", "hedged_attempts", "failures")
            }
            for p, s in d["phases"].items()
        },
    }
    return out


def main() -> None:
    summary = {}
    for name, path in RUNS.items():
        if path.exists():
            summary[name] = analyze(name, json.loads(path.read_text("utf-8")))
    (ROOT / "results/summary.json").write_text(json.dumps(summary, indent=1), encoding="utf-8")
    lines = [
        "| scenario | window | requests | availability | interactive avail. "
        "| interactive p95 ms | deferred done/202 |",
        "|---|---|---|---|---|---|---|",
    ]
    for name, s in summary.items():
        for w, v in s["windows"].items():
            lines.append(
                f"| {name} | {w} | {v['requests']} | {v['availability_pct']}% | "
                f"{v['interactive_availability_pct']}% | {v['interactive_p95_ms']} | "
                f"{v['deferred_completed']}/{v['deferred_202']} |"
            )
    print("\n".join(lines))
    for name, s in summary.items():
        print(f"\n{name}: headline={json.dumps(s['headline'])}")
        for e in s["breaker_timeline_s_from_fault_start"]:
            print(f"  {e['provider']:<11} t={e['t']:>8} {e['transition']:<20} {e['reason']}")


if __name__ == "__main__":
    main()
