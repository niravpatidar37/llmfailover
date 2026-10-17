"""Hard spend cap shared across portfolio projects.

Append-only JSONL ledger (one line per paid call) guarded by a cross-process file lock. Before each paid
call we estimate the worst case (prompt tokens + max_tokens at list price) and refuse with
``BudgetExceeded`` if either the global or the project total would exceed its cap.
"""

from __future__ import annotations

import json
import logging
import os
import secrets
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

import portalocker

_spend_log = logging.getLogger("gateway.spend")


class BudgetExceeded(Exception):
    """Raised before a paid call when the worst-case estimate would break a spend cap."""


@dataclass
class _Totals:
    offset: int = 0
    global_usd: float = 0.0
    by_project: dict[str, float] = field(default_factory=dict)


class SpendLedger:
    def __init__(
        self, path: str | os.PathLike[str], project: str, project_cap_usd: float, global_cap_usd: float
    ):
        self.path = Path(path)
        self.project = project
        self.project_cap_usd = project_cap_usd
        self.global_cap_usd = global_cap_usd
        self._totals = _Totals()
        self._reserved = 0.0  # in-flight worst-case estimates from this process
        self._mu = threading.Lock()

    def _refresh(self) -> _Totals:
        """Incrementally read lines appended since the last refresh (other projects append too)."""
        if not self.path.exists():
            return self._totals
        with self.path.open("rb") as fh:
            portalocker.lock(fh, portalocker.LOCK_EX)  # msvcrt has no shared locks
            try:
                fh.seek(self._totals.offset)
                data = fh.read()
            finally:
                portalocker.unlock(fh)
        end = data.rfind(b"\n") + 1  # only consume complete lines
        for line in data[:end].splitlines():
            try:
                row = json.loads(line)
                usd = float(row.get("usd", 0.0))
            except (ValueError, TypeError):
                continue
            self._totals.global_usd += usd
            proj = str(row.get("project", ""))
            self._totals.by_project[proj] = self._totals.by_project.get(proj, 0.0) + usd
        self._totals.offset += end
        return self._totals

    def totals(self) -> tuple[float, float]:
        with self._mu:
            t = self._refresh()
            return t.global_usd, t.by_project.get(self.project, 0.0)

    def reserve(self, estimate_usd: float) -> None:
        """Check the caps including other in-flight reservations; raise BudgetExceeded on breach."""
        with self._mu:
            t = self._refresh()
            proj = t.by_project.get(self.project, 0.0)
            if t.global_usd + self._reserved + estimate_usd > self.global_cap_usd:
                raise BudgetExceeded(f"global cap {self.global_cap_usd} USD would be exceeded")
            if proj + self._reserved + estimate_usd > self.project_cap_usd:
                raise BudgetExceeded(f"project cap {self.project_cap_usd} USD would be exceeded")
            self._reserved += estimate_usd

    def release(self, estimate_usd: float) -> None:
        with self._mu:
            self._reserved = max(0.0, self._reserved - estimate_usd)

    def record(
        self,
        provider: str,
        model: str,
        input_tokens: int,
        output_tokens: int,
        usd: float,
        note: str | None = None,
    ) -> None:
        row: dict[str, object] = {
            "ts": time.time(),
            "project": self.project,
            "provider": provider,
            "model": model,
            "input_tokens": int(input_tokens),
            "output_tokens": int(output_tokens),
            "usd": round(usd, 10),
        }
        if note:
            row["note"] = note
        self.path.parent.mkdir(parents=True, exist_ok=True)
        line = (json.dumps(row) + "\n").encode("utf-8")
        with self.path.open("ab") as fh:
            portalocker.lock(fh, portalocker.LOCK_EX)
            try:
                fh.write(line)
                fh.flush()
            finally:
                portalocker.unlock(fh)


def estimate_tokens(text_chars: int) -> int:
    """Conservative token estimate: ~3 chars per token plus chat-template overhead."""
    return text_chars // 3 + 100


# ---- async spend guards used by the router ---------------------------------------------------------------
# The router talks to a ``SpendGuard``: reserve the worst-case estimate before a paid call, then either
# ``settle`` (record the real cost and drop the reservation) or ``release`` (no cost, e.g. the call failed).


class SpendGuard(Protocol):
    async def reserve(self, estimate_usd: float) -> str: ...

    async def release(self, reservation: str) -> None: ...

    async def settle(
        self,
        reservation: str,
        provider: str,
        model: str,
        input_tokens: int,
        output_tokens: int,
        usd: float,
        note: str | None = None,
    ) -> None: ...


class FileSpendGuard:
    """Local dev/test: the shared cross-project JSONL ledger (one process; caps across the six projects)."""

    def __init__(self, ledger: SpendLedger) -> None:
        self.ledger = ledger
        self._held: dict[str, float] = {}

    async def reserve(self, estimate_usd: float) -> str:
        self.ledger.reserve(estimate_usd)
        token = secrets.token_hex(8)
        self._held[token] = estimate_usd
        return token

    async def release(self, reservation: str) -> None:
        self.ledger.release(self._held.pop(reservation, 0.0))

    async def settle(
        self,
        reservation: str,
        provider: str,
        model: str,
        input_tokens: int,
        output_tokens: int,
        usd: float,
        note: str | None = None,
    ) -> None:
        await self.release(reservation)
        self.ledger.record(provider, model, input_tokens, output_tokens, usd, note=note)


# KEYS: committed (string, USD this period), reservations zset (token -> expiry), amounts hash (token -> USD)
# ARGV: now, estimate, cap, ttl_s, token. Returns 1 if reserved, 0 if the cap would be exceeded.
_RESERVE = """
local committed, res, amt = KEYS[1], KEYS[2], KEYS[3]
local now = tonumber(ARGV[1])
local expired = redis.call('ZRANGEBYSCORE', res, '-inf', now)
for _, t in ipairs(expired) do redis.call('HDEL', amt, t) end
redis.call('ZREMRANGEBYSCORE', res, '-inf', now)
local total = tonumber(redis.call('GET', committed) or '0')
for _, v in ipairs(redis.call('HVALS', amt)) do total = total + tonumber(v) end
if total + tonumber(ARGV[2]) > tonumber(ARGV[3]) then return 0 end
redis.call('ZADD', res, now + tonumber(ARGV[4]), ARGV[5])
redis.call('HSET', amt, ARGV[5], ARGV[2])
return 1
"""

# KEYS: committed, reservations, amounts, audit list. ARGV: token, usd, row_json, keep, committed_ttl_s
_SETTLE = """
redis.call('ZREM', KEYS[2], ARGV[1])
redis.call('HDEL', KEYS[3], ARGV[1])
redis.call('INCRBYFLOAT', KEYS[1], ARGV[2])
redis.call('EXPIRE', KEYS[1], tonumber(ARGV[5]))
redis.call('RPUSH', KEYS[4], ARGV[3])
redis.call('LTRIM', KEYS[4], -tonumber(ARGV[4]), -1)
return 1
"""


class RedisSpendLedger:
    """Deployed environments: one hard cap shared by every gateway instance, enforced atomically in Redis.

    The cap applies per calendar month (UTC). Reservations carry a TTL, so an instance that dies mid-call
    cannot hold budget forever (until then the leak errs on the safe side: it blocks spend, never allows
    extra). Every settled call is also appended to a capped audit list and logged as JSON, so Cloud Logging
    keeps a durable copy even if Redis loses data.
    """

    AUDIT_KEEP = 100_000

    def __init__(self, redis: Any, project: str, cap_usd: float, reservation_ttl_s: float = 300.0) -> None:
        self.redis = redis
        self.project = project
        self.cap_usd = cap_usd
        self.ttl_s = reservation_ttl_s
        self._reserve = redis.register_script(_RESERVE)
        self._settle = redis.register_script(_SETTLE)

    @staticmethod
    def period(now: float | None = None) -> str:
        return time.strftime("%Y%m", time.gmtime(now if now is not None else time.time()))

    def _keys(self, now: float | None = None) -> list[str]:
        base = f"gw:spend:{self.project}"
        return [f"{base}:committed:{self.period(now)}", f"{base}:res", f"{base}:res:amt", f"{base}:audit"]

    async def reserve(self, estimate_usd: float, now: float | None = None) -> str:
        t = now if now is not None else time.time()
        token = secrets.token_hex(8)
        ok = await self._reserve(
            keys=self._keys(t)[:3],
            args=[repr(t), repr(float(estimate_usd)), repr(self.cap_usd), self.ttl_s, token],
        )
        if not int(ok):
            raise BudgetExceeded(f"monthly cap {self.cap_usd} USD for {self.project} would be exceeded")
        return token

    async def release(self, reservation: str) -> None:
        k = self._keys()
        async with self.redis.pipeline(transaction=True) as pipe:
            pipe.zrem(k[1], reservation)
            pipe.hdel(k[2], reservation)
            await pipe.execute()

    async def settle(
        self,
        reservation: str,
        provider: str,
        model: str,
        input_tokens: int,
        output_tokens: int,
        usd: float,
        note: str | None = None,
    ) -> None:
        row: dict[str, object] = {
            "ts": time.time(),
            "project": self.project,
            "provider": provider,
            "model": model,
            "input_tokens": int(input_tokens),
            "output_tokens": int(output_tokens),
            "usd": round(usd, 10),
        }
        if note:
            row["note"] = note
        await self._settle(
            keys=self._keys(),
            args=[reservation, repr(round(usd, 10)), json.dumps(row), self.AUDIT_KEEP, 40 * 24 * 3600],
        )
        _spend_log.info(json.dumps({"event": "paid_call", **row}))

    async def committed_usd(self, now: float | None = None) -> float:
        v = await self.redis.get(self._keys(now)[0])
        return float(v) if v is not None else 0.0
