"""Redis client construction from env-only settings (TLS + AUTH for Memorystore in deployed envs)."""

from __future__ import annotations

from typing import Any

from redis.asyncio import Redis

from .config import Settings


def make_redis(settings: Settings) -> Redis:
    """``rediss://`` turns on TLS; the Memorystore server CA (not a secret) comes from REDIS_CA_CERT_PEM and
    the AUTH string from REDIS_PASSWORD (Secret Manager). Timeouts keep a sick Redis from hanging requests."""
    kwargs: dict[str, Any] = {
        "socket_timeout": 2.0,
        "socket_connect_timeout": 2.0,
        "health_check_interval": 15,
    }
    if settings.redis_password:
        kwargs["password"] = settings.redis_password
    if settings.redis_url.startswith("rediss://") and settings.redis_ca_pem:
        kwargs["ssl_ca_data"] = settings.redis_ca_pem
    return Redis.from_url(settings.redis_url, **kwargs)
