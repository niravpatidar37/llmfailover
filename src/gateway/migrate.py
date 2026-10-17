"""Apply versioned Redis keyspace migrations. Run as a one-off step/job, never at import time.

python -m gateway.migrate          # apply pending migrations (idempotent; safe to re-run)
python -m gateway.migrate --check  # exit 1 if the store is behind REQUIRED_VERSION
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import secrets
import sys
import time
from typing import Any

from .config import Settings, load_secrets
from .migrations import MIGRATIONS, REQUIRED_VERSION

VERSION_KEY = "gw:schema:version"
APPLIED_KEY = "gw:schema:applied"
LOCK_KEY = "gw:schema:lock"
LOCK_TTL_S = 300

_UNLOCK = """
if redis.call('GET', KEYS[1]) == ARGV[1] then return redis.call('DEL', KEYS[1]) end
return 0
"""

log = logging.getLogger("gateway.migrate")


class MigrationLocked(RuntimeError):
    pass


async def current_version(redis: Any) -> int:
    v = await redis.get(VERSION_KEY)
    return int(v) if v is not None else 0


async def apply(redis: Any) -> list[int]:
    """Apply pending migrations in order under a Redis lock. Returns the versions applied now."""
    token = secrets.token_hex(8)
    if not await redis.set(LOCK_KEY, token, nx=True, ex=LOCK_TTL_S):
        raise MigrationLocked("another migration run holds the lock")
    applied: list[int] = []
    try:
        cur = await current_version(redis)
        for m in MIGRATIONS:
            if m.version <= cur:
                continue
            await m.up(redis)
            async with redis.pipeline(transaction=True) as pipe:
                pipe.hset(APPLIED_KEY, str(m.version), json.dumps({"name": m.name, "ts": time.time()}))
                pipe.set(VERSION_KEY, str(m.version))
                await pipe.execute()
            applied.append(m.version)
            log.info(json.dumps({"event": "migration_applied", "version": m.version, "name": m.name}))
    finally:
        await redis.eval(_UNLOCK, 1, LOCK_KEY, token)
    return applied


async def _main(check: bool) -> int:
    from .logging_setup import configure_logging
    from .redis_client import make_redis

    load_secrets()
    configure_logging()
    redis = make_redis(Settings.from_env())
    try:
        if check:
            cur = await current_version(redis)
            print(json.dumps({"current": cur, "required": REQUIRED_VERSION}))
            return 0 if cur >= REQUIRED_VERSION else 1
        done = await apply(redis)
        print(json.dumps({"applied": done, "version": await current_version(redis)}))
        return 0
    finally:
        await redis.aclose()


def main() -> None:  # pragma: no cover - process entry point
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--check", action="store_true")
    sys.exit(asyncio.run(_main(ap.parse_args().check)))


if __name__ == "__main__":  # pragma: no cover
    main()
