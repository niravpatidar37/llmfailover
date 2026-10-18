"""Live chaos demo against a RUNNING local stack (dev only). Used to record assets/demo/demo.gif.

Needs: redis (:16380), host Ollama (llama3.1:8b) + the ollama-cpu container (qwen2.5:0.5b), the gateway on
:8103 with CHAOS_ENABLED=1 and CHAOS_PROXY_URL=http://127.0.0.1:8113, and the chaos proxy on :8113. Tokens are
read from the gitignored .env and are never printed; neither is the tenant key.

What it does, all against the real services:
1. Sends `classify` traffic (SST-2 sentences) while healthy: served by `ollama`.
2. Tells the chaos proxy to answer every `ollama` call with HTTP 503. Requests fail over; the breaker trips.
3. Clears the fault. The breaker goes half-open after its cooldown, live probes succeed, and it closes.
4. Re-sends an already-answered request_id: the gateway replays the stored answer and its per-request commit
   counter (`/admin/side-effects`) stays at 1.
Breaker transitions are read from the gateway's own log (`/admin/breaker/events`), not inferred by the client.
"""

from __future__ import annotations

import asyncio
import json
import sys
import time
import uuid
from collections import Counter
from pathlib import Path
from typing import Any

import httpx
from dotenv import dotenv_values

ROOT = Path(__file__).resolve().parents[2]
ENV = dotenv_values(ROOT / ".env")
GW, CHAOS = "http://127.0.0.1:8103", "http://127.0.0.1:8113"
ADMIN = {"X-Admin-Token": ENV.get("GATEWAY_ADMIN_TOKEN") or ""}
CHAOS_H = {"X-Chaos-Token": ENV.get("CHAOS_ADMIN_TOKEN") or ""}
PROMPTS = [
    json.loads(x)["sentence"]
    for x in (ROOT / "scripts/prompts/sst2_validation_100.jsonl").read_text("utf-8").splitlines()
]

DIM, B, RED, GRN, YEL, CYN, MAG, R = (
    "\x1b[2m",
    "\x1b[1m",
    "\x1b[31m",
    "\x1b[32m",
    "\x1b[33m",
    "\x1b[36m",
    "\x1b[35m",
    "\x1b[0m",
)
STATE_COL = {"closed": GRN, "open": RED, "half_open": YEL}
T0 = time.monotonic()


def out(s: str = "") -> None:
    sys.stdout.write(s + "\n")
    sys.stdout.flush()


def ts() -> str:
    return f"{DIM}{time.monotonic() - T0:5.1f}s{R}"


class Demo:
    def __init__(self) -> None:
        self.c = httpx.AsyncClient(timeout=60)
        self.run = uuid.uuid4().hex[:4]
        self.n = 0
        self.ok = 0
        self.sent = 0
        self.served: Counter[str] = Counter()
        self.first_rid: tuple[str, dict[str, Any], str] | None = None
        self.since = time.time()
        self.state = {"ollama": "closed"}
        self.key = ""

    async def setup(self) -> None:
        r = await self.c.post(f"{GW}/admin/tenants", json={"tenant": "demo", "rpm": 6000}, headers=ADMIN)
        r.raise_for_status()
        self.key = r.json()["api_key"]  # never printed
        await self.c.delete(f"{CHAOS}/admin/faults/ollama", headers=CHAOS_H)
        provs = (await self.c.get(f"{GW}/admin/providers", headers=ADMIN)).json()["providers"]
        line = "  ".join(f"{p['provider']} {STATE_COL.get(p['breaker'], '')}{p['breaker']}{R}" for p in provs)
        out(f"{ts()} breakers: {line}")
        self.since = time.time()

    async def send(
        self, rid: str | None = None, body: dict[str, Any] | None = None, tag: str = ""
    ) -> httpx.Response:
        if body is None:
            self.n += 1
            rid = f"demo-{self.run}-{self.n:02d}"
            body = {
                "messages": [
                    {
                        "role": "user",
                        "content": f"Sentiment of this movie review, positive or negative? One word.\nReview: {PROMPTS[self.n % len(PROMPTS)]}",
                    }
                ],
                "max_tokens": 4,
                "metadata": {
                    "tenant": "demo",
                    "feature": "review-sentiment",
                    "request_id": rid,
                    "request_class": "classify",
                },
            }
        assert rid is not None
        t = time.perf_counter()
        self.sent += 1
        r = await self.c.post(
            f"{GW}/v1/chat/completions", json=body, headers={"Authorization": f"Bearer {self.key}"}
        )
        ms = (time.perf_counter() - t) * 1000
        j = r.json()
        g = j.get("gateway", {})
        if r.status_code == 200:
            self.ok += 1
            prov = g.get("provider", "?")
            self.served[prov] += 1
            if self.first_rid is None:
                self.first_rid = (rid, body, j["choices"][0]["message"]["content"])
            att = g.get("attempts", [])
            tags = []
            for a in att:
                o, pv = a.get("outcome"), a["provider"]
                if o == "ok":
                    if a.get("probe"):
                        tags.append(f"{YEL}half-open probe ok{R}")
                    if a.get("hedge"):
                        tags.append(f"{DIM}hedge won{R}")
                elif o == "skipped_open":
                    tags.append(f"{RED}{pv} open: skipped{R}")
                elif o == "skipped_half_open":
                    tags.append(f"{YEL}{pv} half-open: skipped{R}")
                elif o == "cancelled":
                    tags.append(f"{DIM}{pv} lost hedge race{R}")
                else:
                    tags.append(f"{RED}{pv} {o}{R}")
            col = GRN if prov == "ollama" else CYN
            ans = (j["choices"][0]["message"]["content"] or "").strip().split()[:1]
            note = ", ".join(tags)
            out(
                f"{ts()} {rid[-2:]} {r.status_code} {col}{prov:<10}{R} {ms:5.0f} ms  {DIM}{(ans or [''])[0][:9]:<9}{R} {note}{tag}"
            )
        else:
            out(f"{ts()} {rid[-2:]} {RED}{r.status_code}{R} {json.dumps(j.get('error', j))[:70]}")
        return r

    async def watch_breakers(self, stop: asyncio.Event) -> None:
        while not stop.is_set():
            ev = (
                await self.c.get(f"{GW}/admin/breaker/events", params={"since": self.since}, headers=ADMIN)
            ).json()["events"]
            for e in ev:
                if e["ts"] <= self.since:
                    continue
                self.since = e["ts"]
                if e["provider"] == "ollama":
                    self.state["ollama"] = e["to"]
                col = STATE_COL.get(e["to"], "")
                out(
                    f"{ts()} {B}{MAG}BREAKER{R} {e['provider']}: {e['from']} -> {col}{B}{e['to']}{R} ({e['reason']})"
                )
            await asyncio.sleep(0.25)

    async def traffic(self, until: Any, rps: float = 3.0) -> None:
        tasks: list[asyncio.Task[httpx.Response]] = []
        while not until():
            tasks.append(asyncio.create_task(self.send()))
            await asyncio.sleep(1 / rps)
        await asyncio.gather(*tasks)

    async def main(self) -> None:
        await self.setup()
        stop = asyncio.Event()
        watcher = asyncio.create_task(self.watch_breakers(stop))

        out(f"\n{B}healthy:{R} classify traffic prefers ollama (GPU) -> ollama_cpu -> groq")
        t = time.monotonic()
        await self.traffic(lambda: time.monotonic() - t > 3, rps=1.2)

        out(f"\n{B}{RED}chaos proxy: every ollama call now returns HTTP 503{R}")
        await self.c.put(
            f"{CHAOS}/admin/faults/ollama", json={"error_rate": 1.0, "status_code": 503}, headers=CHAOS_H
        )
        t = time.monotonic()
        tripped: list[float] = []

        def outage_done() -> bool:
            if self.state["ollama"] == "open" and not tripped:
                tripped.append(time.monotonic())
            return bool(tripped and time.monotonic() - tripped[0] > 4) or time.monotonic() - t > 40

        await self.traffic(outage_done, rps=1.2)

        out(f"\n{B}{GRN}chaos proxy: fault cleared{R} (breaker stays open until its cooldown, then probes)")
        await self.c.delete(f"{CHAOS}/admin/faults/ollama", headers=CHAOS_H)
        t = time.monotonic()
        await self.traffic(lambda: self.state["ollama"] == "closed" or time.monotonic() - t > 60, rps=1.2)
        await asyncio.sleep(0.6)
        stop.set()
        await watcher

        assert self.first_rid is not None
        rid, body, answer = self.first_rid
        out(f"\n{B}idempotency:{R} re-send request_id {rid} (already answered) three times")
        rs = await asyncio.gather(
            *(
                self.c.post(
                    f"{GW}/v1/chat/completions", json=body, headers={"Authorization": f"Bearer {self.key}"}
                )
                for _ in range(3)
            )
        )
        for r in rs:
            same = r.json()["choices"][0]["message"]["content"] == answer
            out(
                f"{ts()} {r.status_code} X-Idempotent-Replay: {r.headers.get('X-Idempotent-Replay')}  same answer: {same}"
            )
        counts = (
            await self.c.post(
                f"{GW}/admin/side-effects", json={"tenant": "demo", "request_ids": [rid]}, headers=ADMIN
            )
        ).json()["counts"]
        out(f"{ts()} commits for {rid}: {B}{counts}{R}")

        out(f"\n{B}summary:{R} {self.ok}/{self.sent} answered (HTTP 200), served by {dict(self.served)}")
        await self.c.aclose()


if __name__ == "__main__":
    asyncio.run(Demo().main())
