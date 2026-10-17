"""Per-provider sliding-window health stored in Redis.

Each attempt is a member of a sorted set scored by its timestamp (ms). Because the window lives in Redis,
a gateway restart keeps its view of provider health.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from typing import Any

from redis.asyncio import Redis

from .errors import NOT_PROVIDER_HEALTH, ErrorType

PREFIX = "gw:health:"


def _pct(sorted_vals: list[float], q: float) -> float | None:
    if not sorted_vals:
        return None
    idx = min(len(sorted_vals) - 1, max(0, round(q * (len(sorted_vals) - 1))))
    return sorted_vals[idx]


@dataclass
class WindowStats:
    provider: str
    samples: int = 0
    successes: int = 0
    health_failures: int = 0
    errors: dict[str, int] = field(default_factory=dict)
    p50_ms: float | None = None
    p95_ms: float | None = None
    p99_ms: float | None = None

    @property
    def success_rate(self) -> float | None:
        judged = self.successes + self.health_failures
        return None if judged == 0 else self.successes / judged

    @property
    def error_rate(self) -> float | None:
        sr = self.success_rate
        return None if sr is None else 1.0 - sr

    def as_dict(self) -> dict[str, Any]:
        return {
            "provider": self.provider,
            "samples": self.samples,
            "successes": self.successes,
            "success_rate": self.success_rate,
            "errors": self.errors,
            "p50_ms": self.p50_ms,
            "p95_ms": self.p95_ms,
            "p99_ms": self.p99_ms,
        }


class HealthTracker:
    def __init__(self, redis: Redis, window_s: float, retention_s: float = 600) -> None:
        self.redis = redis
        self.window_s = window_s
        self.retention_s = retention_s

    async def record(
        self,
        provider: str,
        ok: bool,
        latency_ms: float,
        error_type: ErrorType | None = None,
        now: float | None = None,
    ) -> None:
        ts = now if now is not None else time.time()
        member = (
            f"{int(ts * 1000)}|{uuid.uuid4().hex[:8]}|{1 if ok else 0}|{latency_ms:.1f}|"
            f"{error_type.value if error_type else ''}"
        )
        key = PREFIX + provider
        async with self.redis.pipeline(transaction=False) as pipe:
            pipe.zadd(key, {member: ts * 1000})
            pipe.zremrangebyscore(key, 0, (ts - self.retention_s) * 1000)
            pipe.expire(key, int(self.retention_s) + 60)
            await pipe.execute()

    async def stats(self, provider: str, since: float | None = None, now: float | None = None) -> WindowStats:
        """Stats over the window, optionally starting no earlier than ``since`` (e.g. last breaker change)."""
        t = now if now is not None else time.time()
        start = t - self.window_s
        if since is not None:
            start = max(start, since)
        raw = await self.redis.zrangebyscore(PREFIX + provider, start * 1000, t * 1000)
        st = WindowStats(provider=provider)
        lat: list[float] = []
        for item in raw:
            s = item.decode() if isinstance(item, bytes) else str(item)
            parts = s.split("|")
            if len(parts) != 5:
                continue
            st.samples += 1
            ok = parts[2] == "1"
            if ok:
                st.successes += 1
                lat.append(float(parts[3]))
            else:
                et = parts[4] or ErrorType.OTHER.value
                st.errors[et] = st.errors.get(et, 0) + 1
                if ErrorType(et) not in NOT_PROVIDER_HEALTH:
                    st.health_failures += 1
        lat.sort()
        st.p50_ms, st.p95_ms, st.p99_ms = _pct(lat, 0.50), _pct(lat, 0.95), _pct(lat, 0.99)
        return st
