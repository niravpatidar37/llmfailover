"""Per-provider circuit breaker (closed / open / half-open), shared by every gateway instance through Redis.

* closed -> open: error rate >= threshold, or p95 latency > the provider's budget, over the sliding window
  (counting only samples since the last state change, so a recovered provider is judged on fresh data).
* open -> half_open: after a cooldown that doubles after every failed probe (capped).
* half_open: only ``half_open_probe_ratio`` of eligible requests are let through as probes, and at most
  ``MAX_PROBES_IN_FLIGHT`` probes are in flight **across all gateway instances**. ``half_open_successes``
  consecutive successes close the breaker; any probe failure (or a probe slower than the latency budget)
  reopens it immediately.

Multi-instance safety: every state transition and every probe admission runs inside a Redis Lua script, so
it is atomic with respect to all other instances. Concretely:

* the breaker hash carries an ``epoch`` that increments on every transition. A closed -> open trip is a
  compare-and-set on (state == closed AND epoch == the epoch the caller judged), so two instances that both
  see the failures produce exactly one transition and one event;
* probes are lease tokens in a per-provider sorted set (score = lease expiry). Admission counts live leases
  inside the script, so the in-flight cap holds globally; a crashed instance's lease expires after
  ``probe_lease_s``; a transition deletes all leases, so a late result from an earlier half-open episode is
  ignored instead of counting toward closing the breaker.

Timestamps are passed in by the caller (``time.time()`` on the instance). Cloud Run hosts are NTP-synced;
skew of a few ms only shifts cooldown expiry by that amount.
"""

from __future__ import annotations

import json
import random
import secrets
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from redis.asyncio import Redis

from .config import BreakerPolicy
from .errors import NOT_PROVIDER_HEALTH, ErrorType
from .health import HealthTracker
from .metrics import BREAKER_STATE_VALUE, Metrics

KEY = "gw:breaker:"
EVENTS_KEY = "gw:breaker:events"
MAX_PROBES_IN_FLIGHT = 2
EVENTS_KEEP = 5000

CLOSED, OPEN, HALF_OPEN = "closed", "open", "half_open"

# Shared Lua prelude. KEYS[1] = breaker hash, KEYS[2] = probe lease zset, KEYS[3] = events list.
# ``ensure`` initialises a missing hash; ``transition`` is the ONLY place state changes. The last ARGV of
# every script is the number of events to keep.
_PRELUDE = """
local h, probes, events = KEYS[1], KEYS[2], KEYS[3]
local function ensure(default_cd)
  if redis.call('EXISTS', h) == 0 then
    redis.call('HSET', h, 'state', 'closed', 'changed_at', '0', 'opened_at', '0',
               'cooldown_s', default_cd, 'probe_successes', '0', 'epoch', '0')
  end
end
local function transition(provider, now, to, reason, extra)
  local frm = redis.call('HGET', h, 'state') or 'closed'
  local epoch = tonumber(redis.call('HGET', h, 'epoch') or '0') + 1
  redis.call('HSET', h, 'state', to, 'changed_at', now, 'probe_successes', '0', 'epoch', tostring(epoch))
  if to == 'open' then redis.call('HSET', h, 'opened_at', now) end
  redis.call('DEL', probes)
  redis.call('RPUSH', events, '{"ts":' .. now .. ',"provider":"' .. provider .. '","from":"' .. frm ..
             '","to":"' .. to .. '","reason":"' .. reason .. '"' .. extra .. '}')
  redis.call('LTRIM', events, -tonumber(ARGV[#ARGV]), -1)
  return frm
end
"""

# ARGV: provider, now, default_cd, mode ('peek'|'admit'), draw, ratio, max_probes, lease_s, token, keep
# Returns {allowed (0/1), probe (0/1), state, transitioned_from or ''}
_ADMIT = (
    _PRELUDE
    + """
local provider, now, default_cd, mode = ARGV[1], ARGV[2], ARGV[3], ARGV[4]
ensure(default_cd)
local st = redis.call('HGET', h, 'state')
local moved = ''
if st == 'open' then
  local opened = tonumber(redis.call('HGET', h, 'opened_at'))
  local cd = tonumber(redis.call('HGET', h, 'cooldown_s'))
  if tonumber(now) - opened >= cd then
    moved = transition(provider, now, 'half_open', 'cooldown_elapsed', '')
    st = 'half_open'
  end
end
if mode == 'peek' or st == 'open' then return {0, 0, st, moved} end
if st == 'closed' then return {1, 0, st, moved} end
redis.call('ZREMRANGEBYSCORE', probes, '-inf', now)
if redis.call('ZCARD', probes) >= tonumber(ARGV[7]) or tonumber(ARGV[5]) >= tonumber(ARGV[6]) then
  return {0, 0, st, moved}
end
redis.call('ZADD', probes, tonumber(now) + tonumber(ARGV[8]), ARGV[9])
return {1, 1, st, moved}
"""
)

# ARGV: provider, now, token, ok_fast (1/0), reason, successes_needed, default_cd, max_cd, extra, keep
# Returns {outcome, transitioned_to or ''}; outcome is 'stale' if the lease is gone (episode changed).
_PROBE_RESULT = (
    _PRELUDE
    + """
local provider, now, token = ARGV[1], ARGV[2], ARGV[3]
if redis.call('ZREM', probes, token) == 0 then return {'stale', ''} end
if redis.call('HGET', h, 'state') ~= 'half_open' then return {'stale', ''} end
if ARGV[4] == '1' then
  local ps = redis.call('HINCRBY', h, 'probe_successes', 1)
  if ps >= tonumber(ARGV[6]) then
    redis.call('HSET', h, 'cooldown_s', ARGV[7])
    transition(provider, now, 'closed', 'probes_succeeded', '')
    return {'counted', 'closed'}
  end
  return {'counted', ''}
end
local cd = math.min(tonumber(ARGV[8]), tonumber(redis.call('HGET', h, 'cooldown_s')) * 2)
redis.call('HSET', h, 'cooldown_s', tostring(cd))
transition(provider, now, 'open', ARGV[5], ARGV[9])
return {'counted', 'open'}
"""
)

# ARGV: provider, now, expected_epoch, reason, extra, default_cd, keep. Returns 1 if this call tripped it.
_TRIP = (
    _PRELUDE
    + """
local provider, now = ARGV[1], ARGV[2]
ensure(ARGV[6])
if redis.call('HGET', h, 'state') ~= 'closed' then return 0 end
if (redis.call('HGET', h, 'epoch') or '0') ~= ARGV[3] then return 0 end
transition(provider, now, 'open', ARGV[4], ARGV[5])
return 1
"""
)

# ARGV: provider, now, to, reason, default_cd, keep. Returns the previous state.
_FORCE = (
    _PRELUDE
    + """
ensure(ARGV[5])
if ARGV[3] == 'closed' then redis.call('HSET', h, 'cooldown_s', ARGV[5]) end
return transition(ARGV[1], ARGV[2], ARGV[3], ARGV[4], '')
"""
)


def _s(v: Any) -> str:
    return v.decode() if isinstance(v, bytes) else str(v)


def _extra(d: dict[str, Any] | None) -> str:
    """Pre-encoded JSON members appended to the event object built inside Lua (',"k":v,...')."""
    if not d:
        return ""
    return "," + json.dumps(d, separators=(",", ":"))[1:-1]


@dataclass
class Admission:
    allowed: bool
    probe: bool = False
    state: str = CLOSED
    token: str | None = None


class CircuitBreakers:
    def __init__(
        self,
        redis: Redis,
        health: HealthTracker,
        policy: BreakerPolicy,
        latency_budget_ms: dict[str, float],
        metrics: Metrics,
        rng: Callable[[], float] = random.random,
    ) -> None:
        self.redis = redis
        self.health = health
        self.policy = policy
        self.budget = latency_budget_ms
        self.metrics = metrics
        self.rng = rng
        self._admit = redis.register_script(_ADMIT)
        self._probe_result = redis.register_script(_PROBE_RESULT)
        self._trip = redis.register_script(_TRIP)
        self._force = redis.register_script(_FORCE)

    @staticmethod
    def _keys(p: str) -> list[str]:
        return [KEY + p, KEY + p + ":probes", EVENTS_KEY]

    def _moved(self, p: str, frm: str, to: str) -> None:
        """Count a transition this instance performed and refresh the local state gauge."""
        self.metrics.breaker_transitions.labels(p, frm, to).inc()
        self.metrics.breaker_state.labels(p).set(BREAKER_STATE_VALUE[to])

    async def _run_admit(self, p: str, mode: str, now: float, token: str = "") -> tuple[bool, bool, str]:
        draw = self.rng() if mode == "admit" else 1.0
        allowed, probe, st, moved = await self._admit(
            keys=self._keys(p),
            args=[
                p,
                repr(now),
                repr(float(self.policy.open_cooldown_s)),
                mode,
                repr(draw),
                repr(float(self.policy.half_open_probe_ratio)),
                MAX_PROBES_IN_FLIGHT,
                repr(float(self.policy.probe_lease_s)),
                token,
                EVENTS_KEEP,
            ],
        )
        state = _s(st)
        if _s(moved):
            self._moved(p, _s(moved), state)
        self.metrics.breaker_state.labels(p).set(BREAKER_STATE_VALUE[state])
        return bool(int(allowed)), bool(int(probe)), state

    async def state(self, p: str, now: float | None = None) -> str:
        """Current state; also performs open -> half_open when the cooldown has elapsed."""
        t = now if now is not None else time.time()
        _, _, st = await self._run_admit(p, "peek", t)
        return st

    async def admit(self, p: str, now: float | None = None) -> Admission:
        t = now if now is not None else time.time()
        token = secrets.token_hex(8)
        allowed, probe, st = await self._run_admit(p, "admit", t, token)
        return Admission(allowed, probe, st, token if probe else None)

    async def release_probe(self, p: str, token: str | None) -> None:
        """Give a probe slot back without judging the provider (cancelled, budget refusal, bad request)."""
        if token:
            await self.redis.zrem(KEY + p + ":probes", token)

    async def in_flight_probes(self, p: str, now: float | None = None) -> int:
        """Live probe leases for ``p`` across all instances."""
        t = now if now is not None else time.time()
        return int(await self.redis.zcount(KEY + p + ":probes", f"({t!r}", "+inf"))

    async def on_result(
        self,
        p: str,
        ok: bool,
        latency_ms: float,
        error_type: ErrorType | None,
        probe: str | None,
        now: float | None = None,
    ) -> None:
        """Judge one provider attempt. ``probe`` is the lease token from ``admit`` (None if not a probe)."""
        t = now if now is not None else time.time()
        if not ok and error_type in NOT_PROVIDER_HEALTH:
            await self.release_probe(p, probe)
            return
        budget = self.budget.get(p, float("inf"))
        if probe:
            fast_ok = ok and latency_ms <= budget
            reason = "probe_failed" if not ok else "probe_too_slow"
            outcome, moved = await self._probe_result(
                keys=self._keys(p),
                args=[
                    p,
                    repr(t),
                    probe,
                    "1" if fast_ok else "0",
                    reason,
                    self.policy.half_open_successes,
                    repr(float(self.policy.open_cooldown_s)),
                    repr(float(self.policy.max_cooldown_s)),
                    _extra({"error_type": error_type.value if error_type else None}) if not fast_ok else "",
                    EVENTS_KEEP,
                ],
            )
            if _s(moved):
                self._moved(p, HALF_OPEN, _s(moved))
            if _s(outcome) == "counted":
                return
            # Stale lease (the breaker moved on since admission): judge it as an ordinary sample below.
        raw = await self.redis.hmget(KEY + p, ["state", "changed_at", "epoch"])
        state, changed_at, epoch = (_s(v) if v is not None else None for v in raw)
        if (state or CLOSED) != CLOSED:
            return
        st = await self.health.stats(p, since=float(changed_at or 0), now=t)
        judged = st.successes + st.health_failures
        er = st.error_rate
        trip_reason: str | None = None
        extra: dict[str, Any] = {}
        if judged >= self.policy.min_samples and er is not None and er >= self.policy.error_rate_threshold:
            trip_reason = "error_rate"
            extra = {"error_rate": round(er, 3), "samples": judged, "errors": st.errors}
        elif st.successes >= self.policy.min_samples and st.p95_ms is not None and st.p95_ms > budget:
            trip_reason, extra = "p95_over_budget", {"p95_ms": st.p95_ms, "budget_ms": budget}
        if trip_reason is None:
            return
        tripped = await self._trip(
            keys=self._keys(p),
            args=[
                p,
                repr(t),
                epoch or "0",
                trip_reason,
                _extra(extra),
                repr(float(self.policy.open_cooldown_s)),
                EVENTS_KEEP,
            ],
        )
        if int(tripped):
            self._moved(p, CLOSED, OPEN)

    async def force(self, p: str, to: str, reason: str = "admin") -> None:
        if to not in BREAKER_STATE_VALUE:
            raise ValueError(f"unknown breaker state {to!r}")
        frm = await self._force(
            keys=self._keys(p),
            args=[p, repr(time.time()), to, reason, repr(float(self.policy.open_cooldown_s)), EVENTS_KEEP],
        )
        self._moved(p, _s(frm), to)

    async def events(self, since: float = 0.0) -> list[dict[str, Any]]:
        raw = await self.redis.lrange(EVENTS_KEY, 0, -1)
        out = [json.loads(r) for r in raw]
        return [e for e in out if e["ts"] >= since]
