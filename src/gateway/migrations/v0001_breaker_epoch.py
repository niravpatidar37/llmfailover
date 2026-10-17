"""Breaker hashes gain an ``epoch`` field (multi-instance compare-and-set). Legacy hashes start at 0.

Before this version, half-open probe counts lived in process memory; they are now lease tokens in
``gw:breaker:<provider>:probes``. Nothing to copy for those: in-memory counts did not survive restarts.
"""

from __future__ import annotations

from typing import Any

VERSION = 1


async def up(redis: Any) -> None:
    async for raw in redis.scan_iter(match="gw:breaker:*", count=200):
        key = raw.decode() if isinstance(raw, bytes) else str(raw)
        if key.endswith(":probes") or key == "gw:breaker:events":
            continue
        if await redis.type(key) in (b"hash", "hash"):
            await redis.hsetnx(key, "epoch", "0")
