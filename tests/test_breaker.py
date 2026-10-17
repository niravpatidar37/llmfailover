from __future__ import annotations

import fakeredis

from gateway.breaker import CLOSED, HALF_OPEN, OPEN, CircuitBreakers
from gateway.config import BreakerPolicy
from gateway.errors import ErrorType
from gateway.health import HealthTracker
from gateway.metrics import Metrics

POLICY = BreakerPolicy(
    window_s=30,
    min_samples=4,
    error_rate_threshold=0.5,
    open_cooldown_s=10,
    max_cooldown_s=40,
    half_open_probe_ratio=1.0,
    half_open_successes=2,
)


def make(
    redis: fakeredis.FakeAsyncRedis, policy: BreakerPolicy = POLICY, rng: float = 0.0
) -> tuple[HealthTracker, CircuitBreakers]:
    h = HealthTracker(redis, policy.window_s)
    b = CircuitBreakers(redis, h, policy, {"p": 1000.0}, Metrics(), rng=lambda: rng)
    return h, b


async def feed(
    h: HealthTracker,
    b: CircuitBreakers,
    ok: bool,
    t: float,
    latency: float = 50,
    et: ErrorType | None = ErrorType.SERVER_ERROR,
    probe: str | None = None,
) -> None:
    et = None if ok else et
    await h.record("p", ok, latency, et, now=t)
    await b.on_result("p", ok, latency, et, probe, now=t)


async def test_window_stats_taxonomy_and_percentiles(redis: fakeredis.FakeAsyncRedis) -> None:
    h, _ = make(redis)
    for i in range(100):
        await h.record("p", True, float(i + 1), now=1000 + i * 0.1)
    await h.record("p", False, 5, ErrorType.RATE_LIMIT, now=1010)
    await h.record("p", False, 5, ErrorType.TIMEOUT, now=1010)
    st = await h.stats("p", now=1011)
    assert st.samples == 102 and st.successes == 100
    assert st.errors == {"rate_limit": 1, "timeout": 1}
    assert st.p50_ms is not None and 49 <= st.p50_ms <= 52
    assert st.p95_ms is not None and 94 <= st.p95_ms <= 96
    assert st.p99_ms is not None and st.p99_ms >= 98
    # sliding: an old sample falls out of the window
    st2 = await h.stats("p", now=1000 + 30 + 5)
    assert st2.samples < 102


async def test_window_survives_new_tracker_instance(redis: fakeredis.FakeAsyncRedis) -> None:
    """Health lives in Redis, so a restarted gateway (new tracker object) keeps its view."""
    h, _ = make(redis)
    await h.record("p", False, 10, ErrorType.SERVER_ERROR, now=100)
    h2 = HealthTracker(redis, 30)
    assert (await h2.stats("p", now=101)).errors == {"server_error": 1}


async def test_trips_on_error_rate(redis: fakeredis.FakeAsyncRedis) -> None:
    h, b = make(redis)
    t = 1000.0
    await feed(h, b, True, t)
    for i in range(3):
        await feed(h, b, False, t + i + 1)
    assert await b.state("p", now=t + 5) == OPEN
    ev = await b.events()
    assert ev[-1]["reason"] == "error_rate" and ev[-1]["to"] == OPEN


async def test_does_not_trip_below_min_samples(redis: fakeredis.FakeAsyncRedis) -> None:
    h, b = make(redis)
    for i in range(3):
        await feed(h, b, False, 1000 + i)
    assert await b.state("p", now=1004) == CLOSED


async def test_content_filter_and_bad_request_do_not_trip(redis: fakeredis.FakeAsyncRedis) -> None:
    h, b = make(redis)
    for i in range(10):
        await feed(h, b, False, 1000 + i, et=ErrorType.CONTENT_FILTER if i % 2 else ErrorType.BAD_REQUEST)
    assert await b.state("p", now=1011) == CLOSED


async def test_trips_on_p95_over_budget(redis: fakeredis.FakeAsyncRedis) -> None:
    h, b = make(redis)
    for i in range(5):
        await feed(h, b, True, 1000 + i, latency=2500)  # budget is 1000 ms
    assert await b.state("p", now=1006) == OPEN
    assert (await b.events())[-1]["reason"] == "p95_over_budget"


async def test_half_open_recovers_and_closes(redis: fakeredis.FakeAsyncRedis) -> None:
    h, b = make(redis)
    for i in range(4):
        await feed(h, b, False, 1000 + i)
    assert await b.state("p", now=1004) == OPEN
    assert not (await b.admit("p", now=1005)).allowed  # open: no traffic
    adm = await b.admit("p", now=1014)  # cooldown 10 s elapsed -> half-open probe
    assert adm.allowed and adm.probe and adm.state == HALF_OPEN
    await feed(h, b, True, 1014.5, probe=adm.token)
    assert await b.state("p", now=1015) == HALF_OPEN
    adm2 = await b.admit("p", now=1015)
    await feed(h, b, True, 1015.5, probe=adm2.token)
    assert await b.state("p", now=1016) == CLOSED
    # Old failures from before the recovery must not immediately re-trip the closed breaker.
    await feed(h, b, True, 1016)
    assert await b.state("p", now=1017) == CLOSED


async def test_half_open_probe_failure_reopens_with_longer_cooldown(redis: fakeredis.FakeAsyncRedis) -> None:
    h, b = make(redis)
    for i in range(4):
        await feed(h, b, False, 1000 + i)
    adm = await b.admit("p", now=1014)
    await feed(h, b, False, 1014.5, probe=adm.token)
    assert await b.state("p", now=1015) == OPEN
    assert await b.state("p", now=1014.5 + 15) == OPEN  # cooldown doubled to 20 s
    assert await b.state("p", now=1014.5 + 21) == HALF_OPEN


async def test_half_open_sends_only_a_share_of_traffic(redis: fakeredis.FakeAsyncRedis) -> None:
    policy = POLICY.model_copy(update={"half_open_probe_ratio": 0.2})
    h, b_hi = make(redis, policy, rng=0.5)  # rng above ratio -> not admitted
    for i in range(4):
        await feed(h, b_hi, False, 1000 + i)
    adm = await b_hi.admit("p", now=1020)
    assert adm.state == HALF_OPEN and not adm.allowed
    _, b_lo = make(redis, policy, rng=0.1)
    assert (await b_lo.admit("p", now=1020)).allowed
