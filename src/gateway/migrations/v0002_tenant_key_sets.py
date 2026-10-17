"""Tenants get a ``gw:tenant:<t>:keys`` set of live key hashes (several per tenant for overlap rotation).

Backfills the set from the single ``key_hash`` field written by ``POST /admin/tenants`` before this version,
so Secret Manager sync can revoke hashes that are no longer listed.
"""

from __future__ import annotations

from typing import Any

VERSION = 2


async def up(redis: Any) -> None:
    async for raw in redis.scan_iter(match="gw:tenant:*", count=200):
        key = raw.decode() if isinstance(raw, bytes) else str(raw)
        if key.endswith(":keys") or await redis.type(key) not in (b"hash", "hash"):
            continue
        h = await redis.hget(key, "key_hash")
        if h:
            await redis.sadd(key + ":keys", h)
