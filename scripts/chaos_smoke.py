"""Dev-only chaos walkthrough against the local stack: one request per class, a duplicate, and an injected
Ollama outage through the chaos proxy. The end-to-end smoke suite is ``scripts/smoke.py``."""

from __future__ import annotations

import json
import os
import time
import uuid
from pathlib import Path

import httpx
from dotenv import dotenv_values

ROOT = Path(__file__).resolve().parents[1]
ENV = dotenv_values(ROOT / ".env")
GW = os.environ.get("GATEWAY_URL", "http://127.0.0.1:8103")
CHAOS = os.environ.get("CHAOS_URL", "http://127.0.0.1:8113")
ADMIN = {"X-Admin-Token": ENV.get("GATEWAY_ADMIN_TOKEN") or ""}
CHAOS_H = {"X-Chaos-Token": ENV.get("CHAOS_ADMIN_TOKEN") or ""}


def main() -> None:
    c = httpx.Client(timeout=60)
    key = c.post(f"{GW}/admin/tenants", json={"tenant": "smoke", "rpm": 600}, headers=ADMIN).json()["api_key"]
    h = {"Authorization": f"Bearer {key}"}

    def send(cls: str, rid: str, content: str = "Classify the sentiment: 'great product'. One word.") -> None:
        body = {
            "messages": [{"role": "user", "content": content}],
            "max_tokens": 8,
            "metadata": {"tenant": "smoke", "feature": "smoke-test", "request_id": rid, "request_class": cls},
        }
        t = time.perf_counter()
        r = c.post(f"{GW}/v1/chat/completions", json=body, headers=h)
        j = r.json()
        g = j.get("gateway", {})
        print(
            cls,
            r.status_code,
            f"{(time.perf_counter() - t) * 1000:.0f}ms",
            g.get("provider"),
            repr(j.get("choices", [{}])[0].get("message", {}).get("content"))
            if r.status_code == 200
            else json.dumps(j)[:300],
            [(a["provider"], a["outcome"]) for a in g.get("attempts", [])],
            r.headers.get("X-Idempotent-Replay"),
        )

    rid = f"smoke-{uuid.uuid4().hex[:12]}"
    for cls in ("classify", "generate", "batch"):
        send(cls, f"{rid}-{cls}")
    send("classify", f"{rid}-classify")  # duplicate -> replay
    print("inject 100% 503 into ollama")
    c.put(f"{CHAOS}/admin/faults/ollama", json={"error_rate": 1.0, "status_code": 503}, headers=CHAOS_H)
    for i in range(6):
        send("classify", f"{rid}-chaos-{i}")
    c.delete(f"{CHAOS}/admin/faults/ollama", headers=CHAOS_H)
    print(json.dumps(c.get(f"{GW}/admin/providers", headers=ADMIN).json(), indent=1)[:2500])


if __name__ == "__main__":
    main()
