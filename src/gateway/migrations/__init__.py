"""Versioned Redis keyspace migrations, applied by ``python -m gateway.migrate`` (a Cloud Run job in deploys).

The gateway has no SQL database; its state is Redis. Changes to the shape of stored keys are shipped as
numbered modules here (``vNNNN_<name>.py``, each with ``VERSION`` and ``async def up(redis)``) and are never
run at import time. The app's /readyz reports not-ready until the store is at ``REQUIRED_VERSION``.
"""

from __future__ import annotations

import importlib
import pkgutil
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class Migration:
    version: int
    name: str
    up: Callable[[Any], Awaitable[None]]


def discover() -> list[Migration]:
    out: list[Migration] = []
    for info in pkgutil.iter_modules(__path__):
        if not info.name.startswith("v"):
            continue
        mod = importlib.import_module(f"{__name__}.{info.name}")
        out.append(Migration(int(mod.VERSION), info.name, mod.up))
    out.sort(key=lambda m: m.version)
    versions = [m.version for m in out]
    if versions != list(range(1, len(out) + 1)):
        raise RuntimeError(f"migration versions must be 1..N without gaps, got {versions}")
    return out


MIGRATIONS = discover()
REQUIRED_VERSION = MIGRATIONS[-1].version if MIGRATIONS else 0
