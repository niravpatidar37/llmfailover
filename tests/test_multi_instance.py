"""Two gateway instances sharing one Redis: breaker state, half-open probe bound and shared limits.

Every test runs twice: against fakeredis (two clients on one FakeServer) and against a real Redis 7 when
REDIS_TEST_URL is set (CI provides a redis service container), so the Lua scripts are exercised on the
real server, not only on the emulator.
"""

from __future__ import annotations

import asyncio
import json
import os
import random
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import httpx
import pytest
from redis.asyncio import Redis

from gateway.app import create_app
from gateway.breaker import CLOSED, HALF_OPEN, MAX_PROBES_IN_FLIGHT, OPEN, CircuitBreakers
from gateway.config import BreakerPolicy, Settings
from gateway.errors import ErrorType
from gateway.health import HealthTracker
from gateway.metrics import Metrics
from gateway.spend import BudgetExceeded, RedisSpendLedger
from gateway.store import SlidingWindow, TenantStore

from .conftest import ADMIN, REAL_REDIS_URL, FakeCaller, chat_body, fail, make_config, ok

POLICY = BreakerPolicy(
    window_s=30,
    min_samples=4,
    error_rate_threshold=0.5,
    open_cooldown_s=10,
    max_cooldown_s=40,
    half_open_probe_ratio=1.0,
    half_open_successes=2,
    probe_lease_s=60,
)


def instance(redis: Any, policy: BreakerPolicy = POLICY) -> tuple[HealthTracker, CircuitBreakers, Metrics]:
    m = Metrics()
    h = HealthTracker(redis, policy.window_s)
    return h, CircuitBreakers(redis, h, policy, {"p": 1000.0}, m, rng=lambda: 0.0), m


async def trip(h: HealthTracker, b: CircuitBreakers, t0: float = 1000.0) -> None:
    for i in range(4):
        await h.record("p", False, 10, ErrorType.SERVER_ERROR, now=t0 + i)
        await b.on_result("p", False, 10, ErrorType.SERVER_ERROR, None, now=t0 + i)


def transitions(m: Metrics, frm: str, to: str) -> float:
    return float(m.breaker_transitions.labels("p", frm, to)._value.get())


# ---------------------------------------------------------------- breaker level


async def test_trip_on_one_instance_is_seen_by_the_other(redis_pair: tuple[Any, Any]) -> None:
    ha, a, _ = instance(redis_pair[0])
    _, b, _ = instance(redis_pair[1])
    await trip(ha, a)
    assert await a.state("p", now=1004) == OPEN
    assert await b.state("p", now=1004) == OPEN
    assert not (await b.admit("p", now=1005)).allowed


async def test_concurrent_trip_is_one_transition(redis_pair: tuple[Any, Any]) -> None:
    """Both instances judge the same failing window at the same time: one closed->open, one event."""
    ha, a, ma = instance(redis_pair[0])
    _, b, mb = instance(redis_pair[1])
    for i in range(6):
        await ha.record("p", False, 10, ErrorType.SERVER_ERROR, now=1000 + i * 0.1)
    calls = [
        x.on_result("p", False, 10, ErrorType.SERVER_ERROR, None, now=1001.0)
        for x in (a, b)
        for _ in range(10)
    ]
    await asyncio.gather(*calls)
    opens = [e for e in await a.events() if e["to"] == OPEN]
    assert len(opens) == 1
    assert transitions(ma, CLOSED, OPEN) + transitions(mb, CLOSED, OPEN) == 1


async def test_trip_judged_on_a_stale_epoch_is_rejected(redis_pair: tuple[Any, Any]) -> None:
    """An instance that judged an OLD closed episode must not trip the breaker after it recovered."""
    ha, a, _ = instance(redis_pair[0])
    _, b, _ = instance(redis_pair[1])
    await trip(ha, a)  # epoch 1 (open)
    p1 = await a.admit("p", now=1020)  # epoch 2 (half_open)
    await a.on_result("p", True, 5, None, p1.token, now=1020.1)
    p2 = await b.admit("p", now=1020.2)
    await b.on_result("p", True, 5, None, p2.token, now=1020.3)  # epoch 3 (closed again)
    stale = await b._trip(
        keys=b._keys("p"), args=["p", "1020.4", "0", "error_rate", "", "10.0", 5000]
    )  # judged at epoch 0
    assert int(stale) == 0
    assert await a.state("p", now=1020.5) == CLOSED


async def test_cooldown_transition_happens_once_across_instances(redis_pair: tuple[Any, Any]) -> None:
    ha, a, _ = instance(redis_pair[0])
    _, b, _ = instance(redis_pair[1])
    await trip(ha, a)
    states = await asyncio.gather(*[x.state("p", now=1020) for x in (a, b) for _ in range(10)])
    assert set(states) == {HALF_OPEN}
    assert len([e for e in await a.events() if e["to"] == HALF_OPEN]) == 1


async def test_half_open_admits_a_bounded_number_of_probes_in_total(redis_pair: tuple[Any, Any]) -> None:
    ha, a, _ = instance(redis_pair[0])
    _, b, _ = instance(redis_pair[1])
    await trip(ha, a)
    admissions = await asyncio.gather(*[x.admit("p", now=1020) for x in (a, b) for _ in range(50)])
    probes = [adm for adm in admissions if adm.allowed]
    assert len(probes) == MAX_PROBES_IN_FLIGHT
    assert all(p.probe and p.token for p in probes)
    assert await b.in_flight_probes("p", now=1020) == MAX_PROBES_IN_FLIGHT
    # Releasing one slot on instance A frees exactly one slot for instance B.
    await a.release_probe("p", probes[0].token)
    again = await asyncio.gather(*[b.admit("p", now=1021) for _ in range(20)])
    assert sum(adm.allowed for adm in again) == 1


@pytest.mark.parametrize("seed", [1, 7, 42, 1234])
async def test_probe_bound_holds_through_a_random_interleaving(
    redis_pair: tuple[Any, Any], seed: int
) -> None:
    """Property: across both instances, admitted-but-unresolved probes never exceed the cap.

    The oracle is a plain set of tokens the test itself saw admitted and not yet resolved; it does not
    read the Redis lease set it is checking.
    """
    policy = POLICY.model_copy(update={"half_open_successes": 10_000})  # keep one long half-open episode
    ha, a, _ = instance(redis_pair[0], policy)
    _, b, _ = instance(redis_pair[1], policy)
    await trip(ha, a)
    rng = random.Random(seed)
    outstanding: list[tuple[CircuitBreakers, str]] = []
    t = 1020.0
    max_seen = 0
    for _ in range(300):
        t += 0.01
        inst = rng.choice((a, b))
        if outstanding and rng.random() < 0.5:
            owner, tok = outstanding.pop(rng.randrange(len(outstanding)))
            if rng.random() < 0.5:
                await owner.on_result("p", True, 5, None, tok, now=t)  # success, does not close (10k needed)
            else:
                await owner.release_probe("p", tok)  # cancelled / non-health error
        else:
            adm = await inst.admit("p", now=t)
            if adm.allowed:
                assert adm.token is not None
                outstanding.append((inst, adm.token))
        max_seen = max(max_seen, len(outstanding))
        assert len(outstanding) <= MAX_PROBES_IN_FLIGHT
    assert max_seen == MAX_PROBES_IN_FLIGHT  # the bound was actually reached, not trivially satisfied


async def test_late_probe_result_from_previous_episode_is_ignored(redis_pair: tuple[Any, Any]) -> None:
    ha, a, _ = instance(redis_pair[0])
    _, b, _ = instance(redis_pair[1])
    await trip(ha, a)
    late = await a.admit("p", now=1014)
    failing = await b.admit("p", now=1014)
    assert late.probe and failing.probe
    await b.on_result("p", False, 10, ErrorType.SERVER_ERROR, failing.token, now=1014.5)  # reopen, cd 20 s
    assert await a.state("p", now=1035) == HALF_OPEN  # second half-open episode
    await a.on_result("p", True, 5, None, late.token, now=1035.1)  # stale lease: must not count
    raw = await redis_pair[0].hget("gw:breaker:p", "probe_successes")
    assert int(raw) == 0
    one = await b.admit("p", now=1035.2)
    await b.on_result("p", True, 5, None, one.token, now=1035.3)
    assert await a.state("p", now=1035.4) == HALF_OPEN  # needs 2 fresh successes, has 1
    two = await a.admit("p", now=1035.5)
    await a.on_result("p", True, 5, None, two.token, now=1035.6)
    assert await b.state("p", now=1035.7) == CLOSED


async def test_crashed_instance_probe_leases_expire(redis_pair: tuple[Any, Any]) -> None:
    ha, a, _ = instance(redis_pair[0])
    _, b, _ = instance(redis_pair[1])
    await trip(ha, a)
    held = [await a.admit("p", now=1020) for _ in range(MAX_PROBES_IN_FLIGHT)]
    assert all(x.allowed for x in held)  # instance A "crashes" holding both slots
    assert not (await b.admit("p", now=1021)).allowed
    assert (await b.admit("p", now=1020 + POLICY.probe_lease_s + 1)).allowed


@pytest.mark.skipif(not REAL_REDIS_URL, reason="needs a real Redis (REDIS_TEST_URL); CI provides one")
async def test_two_processes_share_the_probe_bound() -> None:
    """Two OS processes (not just two clients in one event loop) race for half-open probe slots."""
    r = Redis.from_url(REAL_REDIS_URL)
    await r.flushdb()
    ha, a, _ = instance(r)
    await trip(ha, a)
    await a.state("p", now=1020)  # -> half_open
    script = (
        "import asyncio,os\n"
        "from redis.asyncio import Redis\n"
        "from gateway.breaker import CircuitBreakers\n"
        "from gateway.config import BreakerPolicy\n"
        "from gateway.health import HealthTracker\n"
        "from gateway.metrics import Metrics\n"
        "async def main():\n"
        "    r = Redis.from_url(os.environ['REDIS_TEST_URL'])\n"
        "    pol = BreakerPolicy(half_open_probe_ratio=1.0, open_cooldown_s=10)\n"
        "    b = CircuitBreakers(r, HealthTracker(r, 30), pol, {'p': 1000.0}, Metrics(), rng=lambda: 0.0)\n"
        "    res = await asyncio.gather(*[b.admit('p', now=1021) for _ in range(100)])\n"
        "    print(sum(x.allowed for x in res))\n"
        "    await r.aclose()\n"
        "asyncio.run(main())\n"
    )
    counts = await asyncio.to_thread(_race_in_two_processes, script)
    await r.flushdb()
    await r.aclose()
    assert sum(counts) == MAX_PROBES_IN_FLIGHT


SRC_DIR = str(Path(__file__).resolve().parents[1] / "src")


def _race_in_two_processes(script: str) -> list[int]:
    env = {**os.environ, "PYTHONPATH": SRC_DIR}
    procs = [
        subprocess.Popen([sys.executable, "-c", script], stdout=subprocess.PIPE, env=env)  # noqa: S603
        for _ in range(2)
    ]
    outs = [p.communicate(timeout=60)[0] for p in procs]
    assert all(p.returncode == 0 for p in procs)
    return [int(o.strip()) for o in outs]


# ---------------------------------------------------------------- two full gateway apps


def _two_apps(
    redis_pair: tuple[Any, Any], tmp_path: Path, cooldown: float
) -> tuple[Any, Any, FakeCaller, FakeCaller]:
    cfg = make_config(
        breaker={
            "window_s": 30,
            "min_samples": 4,
            "error_rate_threshold": 0.5,
            "open_cooldown_s": cooldown,
            "max_cooldown_s": 40,
            "half_open_probe_ratio": 1.0,
            "half_open_successes": 2,
        }
    )
    callers = (FakeCaller(), FakeCaller())
    apps = []
    for i, (r, c) in enumerate(zip(redis_pair, callers, strict=True)):
        st = Settings(
            redis_url="redis://unused", admin_token=ADMIN, ledger_path=str(tmp_path / f"l{i}.jsonl")
        )
        apps.append(create_app(cfg=cfg, settings=st, redis=r, caller=c, start_background=False))
    return apps[0], apps[1], callers[0], callers[1]


async def _tenant(c: httpx.AsyncClient) -> str:
    r = await c.post(
        "/admin/tenants", json={"tenant": "acme", "rpm": 10_000}, headers={"X-Admin-Token": ADMIN}
    )
    return str(r.json()["api_key"])


async def test_two_gateway_apps_share_breaker_state_and_probe_bound(
    redis_pair: tuple[Any, Any], tmp_path: Path
) -> None:
    app_a, app_b, ca, cb = _two_apps(redis_pair, tmp_path, cooldown=0.4)
    ca.behaviors["a"] = fail()
    async with (
        httpx.AsyncClient(transport=httpx.ASGITransport(app=app_a), base_url="http://a") as ga,
        httpx.AsyncClient(transport=httpx.ASGITransport(app=app_b), base_url="http://b") as gb,
    ):
        key = await _tenant(ga)  # same Redis: the key works on both instances
        hdr = {"Authorization": f"Bearer {key}"}
        for i in range(4):  # instance A sees provider "a" fail 4 times -> trip
            r = await ga.post("/v1/chat/completions", json=chat_body(rid=f"trip-req-{i:04d}"), headers=hdr)
            assert r.status_code == 200 and r.json()["gateway"]["provider"] == "b"
        # Instance B never saw a failure, but routes around "a" without calling it.
        r = await gb.post("/v1/chat/completions", json=chat_body(rid="seen-req-0001"), headers=hdr)
        assert r.status_code == 200 and r.json()["gateway"]["provider"] == "b"
        assert "a" not in cb.calls
        # After the cooldown, a burst on BOTH instances gets at most MAX_PROBES_IN_FLIGHT probes in total.
        await asyncio.sleep(0.5)
        ca.behaviors["a"] = ok(delay=0.3)
        cb.behaviors["a"] = ok(delay=0.3)
        n_before = ca.calls.count("a")
        burst = [
            g.post("/v1/chat/completions", json=chat_body(rid=f"probe-{g is ga}-{i:04d}"), headers=hdr)
            for g in (ga, gb)
            for i in range(10)
        ]
        res = await asyncio.gather(*burst)
        assert all(r.status_code == 200 for r in res)
        probes = (ca.calls.count("a") - n_before) + cb.calls.count("a")
        assert probes == MAX_PROBES_IN_FLIGHT
        events = (await ga.get("/admin/breaker/events", headers={"X-Admin-Token": ADMIN})).json()["events"]
        assert [e["to"] for e in events] == [OPEN, HALF_OPEN, CLOSED]  # one shared timeline, no duplicates


# ---------------------------------------------------------------- shared limits


async def test_tenant_rate_limit_holds_across_instances(redis_pair: tuple[Any, Any]) -> None:
    ta, tb = TenantStore(redis_pair[0]), TenantStore(redis_pair[1])
    await redis_pair[0].hset("gw:tenant:acme", mapping={"rpm": 10})
    t = time.time()
    res = await asyncio.gather(*[x.allow_request("acme", now=t) for x in (ta, tb) for _ in range(30)])
    assert sum(ok_ for ok_, _ in res) == 10


async def test_hedge_budget_window_is_shared(redis_pair: tuple[Any, Any]) -> None:
    wa, wb = SlidingWindow(redis_pair[0]), SlidingWindow(redis_pair[1])
    t = time.time()
    calls = [w.allow("gw:hedges", 5, 60, t, f"{n}-{i}") for i in range(20) for n, w in enumerate((wa, wb))]
    res = await asyncio.gather(*calls)
    assert sum(ok_ for ok_, _ in res) == 5


async def test_redis_spend_cap_holds_across_instances(redis_pair: tuple[Any, Any]) -> None:
    la = RedisSpendLedger(redis_pair[0], "p3-test", cap_usd=1.0)
    lb = RedisSpendLedger(redis_pair[1], "p3-test", cap_usd=1.0)

    async def try_reserve(led: RedisSpendLedger) -> str | None:
        try:
            return await led.reserve(0.1)
        except BudgetExceeded:
            return None

    toks = await asyncio.gather(*[try_reserve(x) for x in (la, lb) for _ in range(15)])
    held = [tk for tk in toks if tk]
    assert len(held) == 10  # 10 x 0.1 = cap; never more, whichever instance asked
    await la.settle(held[0], "groq", "m", 10, 2, 0.05)  # real cost lower than the estimate
    for tk in held[1:]:
        await lb.release(tk)
    assert await lb.committed_usd() == pytest.approx(0.05)
    audit = [json.loads(x) for x in await redis_pair[0].lrange("gw:spend:p3-test:audit", 0, -1)]
    assert audit[-1]["usd"] == pytest.approx(0.05) and audit[-1]["provider"] == "groq"
    with pytest.raises(BudgetExceeded):
        await la.reserve(0.96)  # 0.05 committed + 0.96 > 1.0


async def test_spend_reservation_of_a_crashed_instance_expires(redis_pair: tuple[Any, Any]) -> None:
    la = RedisSpendLedger(redis_pair[0], "p3-test", cap_usd=1.0, reservation_ttl_s=30)
    lb = RedisSpendLedger(redis_pair[1], "p3-test", cap_usd=1.0, reservation_ttl_s=30)
    t = time.time()
    await la.reserve(0.9, now=t)  # instance A dies before settling
    with pytest.raises(BudgetExceeded):
        await lb.reserve(0.2, now=t + 1)
    await lb.reserve(0.2, now=t + 31)  # the dead reservation expired; fail-safe until then
