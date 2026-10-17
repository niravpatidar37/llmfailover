"""Redis-backed stores: tenants and hashed API keys, rate limits, idempotency records, cost attribution."""

from __future__ import annotations

import hashlib
import json
import secrets
import time
from dataclasses import dataclass
from typing import Any

from redis.asyncio import Redis

IDEM_INFLIGHT_TTL_S = 300
IDEM_DONE_TTL_S = 24 * 3600


def hash_key(api_key: str) -> str:
    """API keys are 256-bit random tokens, so an unsalted SHA-256 is a sufficient one-way index."""
    return hashlib.sha256(api_key.encode("utf-8")).hexdigest()


def _s(v: Any) -> str:
    return v.decode() if isinstance(v, bytes) else str(v)


class TenantStore:
    def __init__(self, redis: Redis) -> None:
        self.redis = redis

    async def create(self, tenant: str, rpm: int) -> str:
        """Create or rotate a tenant key. Only its SHA-256 is stored; the plaintext is returned once."""
        key = "gwk_" + secrets.token_urlsafe(32)
        old = await self.redis.hget(f"gw:tenant:{tenant}", "key_hash")
        async with self.redis.pipeline(transaction=True) as pipe:
            if old:
                pipe.delete(f"gw:apikey:{_s(old)}")
            pipe.set(f"gw:apikey:{hash_key(key)}", tenant)
            pipe.hset(
                f"gw:tenant:{tenant}",
                mapping={"rpm": rpm, "key_hash": hash_key(key), "created_at": time.time()},
            )
            await pipe.execute()
        return key

    async def resolve(self, api_key: str) -> str | None:
        if not api_key or len(api_key) > 200:
            return None
        v = await self.redis.get(f"gw:apikey:{hash_key(api_key)}")
        return _s(v) if v is not None else None

    async def rpm(self, tenant: str) -> int:
        v = await self.redis.hget(f"gw:tenant:{tenant}", "rpm")
        return int(_s(v)) if v is not None else 60

    async def allow_request(self, tenant: str, now: float | None = None) -> tuple[bool, float]:
        """Sliding-log limiter over 60 s. Returns (allowed, retry_after_s)."""
        t = now if now is not None else time.time()
        limit = await self.rpm(tenant)
        key = f"gw:rl:{tenant}"
        async with self.redis.pipeline(transaction=True) as pipe:
            pipe.zremrangebyscore(key, 0, t - 60)
            pipe.zcard(key)
            pipe.zrange(key, 0, 0, withscores=True)
            _, count, oldest = await pipe.execute()
        if int(count) >= limit:
            retry = 60 - (t - float(oldest[0][1])) if oldest else 1.0
            return False, max(0.1, retry)
        async with self.redis.pipeline(transaction=True) as pipe:
            pipe.zadd(key, {f"{t:.6f}:{secrets.token_hex(4)}": t})
            pipe.expire(key, 120)
            await pipe.execute()
        return True, 0.0

    async def feature_label(self, tenant: str, feature: str, max_features: int) -> str:
        """Bound Prometheus label cardinality: past ``max_features`` distinct features, report 'other'."""
        key = f"gw:features:{tenant}"
        if await self.redis.sismember(key, feature):
            return feature
        if int(await self.redis.scard(key)) >= max_features:
            return "other"
        await self.redis.sadd(key, feature)
        return feature


@dataclass
class IdemRecord:
    status: str  # in_flight | queued | done | failed
    fingerprint: str
    response: dict[str, Any] | None = None
    error: str | None = None


class IdempotencyStore:
    """request_id -> result. Claiming uses SET NX so concurrent duplicates cannot both execute."""

    def __init__(self, redis: Redis) -> None:
        self.redis = redis

    @staticmethod
    def _key(tenant: str, rid: str) -> str:
        return f"gw:idem:{tenant}:{rid}"

    async def claim(self, tenant: str, rid: str, fingerprint: str) -> tuple[bool, IdemRecord | None]:
        rec = IdemRecord("in_flight", fingerprint)
        ok = await self.redis.set(
            self._key(tenant, rid), json.dumps(rec.__dict__), nx=True, ex=IDEM_INFLIGHT_TTL_S
        )
        if ok:
            return True, None
        return False, await self.get(tenant, rid)

    async def get(self, tenant: str, rid: str) -> IdemRecord | None:
        raw = await self.redis.get(self._key(tenant, rid))
        if raw is None:
            return None
        return IdemRecord(**json.loads(raw))

    async def set(self, tenant: str, rid: str, rec: IdemRecord, ttl: int = IDEM_DONE_TTL_S) -> None:
        await self.redis.set(self._key(tenant, rid), json.dumps(rec.__dict__), ex=ttl)

    async def release(self, tenant: str, rid: str) -> None:
        """Drop an in-flight claim after a failure with no side effect, so a client retry may execute."""
        await self.redis.delete(self._key(tenant, rid))

    async def commit(self, tenant: str, rid: str, rec: IdemRecord) -> int:
        """Store the final result and count committed executions for this request_id (should stay 1)."""
        async with self.redis.pipeline(transaction=True) as pipe:
            pipe.incr(f"gw:sidefx:{tenant}:{rid}")
            pipe.expire(f"gw:sidefx:{tenant}:{rid}", IDEM_DONE_TTL_S)
            pipe.set(self._key(tenant, rid), json.dumps(rec.__dict__), ex=IDEM_DONE_TTL_S)
            res = await pipe.execute()
        return int(res[0])

    async def side_effects(self, tenant: str, rids: list[str]) -> dict[str, int]:
        if not rids:
            return {}
        vals = await self.redis.mget([f"gw:sidefx:{tenant}:{r}" for r in rids])
        return {r: int(_s(v)) if v is not None else 0 for r, v in zip(rids, vals, strict=True)}


class CostStore:
    async def add(
        self,
        redis: Redis,
        tenant: str,
        feature: str,
        provider: str,
        usd: float,
        input_tokens: int,
        output_tokens: int,
        now: float | None = None,
    ) -> None:
        t = now if now is not None else time.time()
        hour = time.strftime("%Y%m%d%H", time.gmtime(t))
        async with redis.pipeline(transaction=False) as pipe:
            pipe.hincrbyfloat(f"gw:cost:{tenant}", feature, usd)
            pipe.hincrbyfloat(f"gw:cost:hour:{hour}", f"{tenant}|{feature}|{provider}", usd)
            pipe.expire(f"gw:cost:hour:{hour}", 8 * 24 * 3600)
            pipe.hincrby(f"gw:tokens:{tenant}", f"{feature}|in", input_tokens)
            pipe.hincrby(f"gw:tokens:{tenant}", f"{feature}|out", output_tokens)
            pipe.sadd("gw:tenants", tenant)
            await pipe.execute()

    async def summary(self, redis: Redis) -> dict[str, dict[str, float]]:
        out: dict[str, dict[str, float]] = {}
        for t in await redis.smembers("gw:tenants"):
            tenant = _s(t)
            raw = await redis.hgetall(f"gw:cost:{tenant}")
            out[tenant] = {_s(k): round(float(_s(v)), 8) for k, v in raw.items()}
        return out
