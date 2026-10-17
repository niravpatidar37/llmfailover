"""Configuration loading: providers, request classes and policies from a YAML file, plus env-only settings."""

from __future__ import annotations

import ipaddress
import os
import re
from functools import lru_cache
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlparse

import yaml
from dotenv import load_dotenv
from pydantic import BaseModel, Field

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CONFIG = REPO_ROOT / "config" / "gateway.yaml"
PROVIDER_NAME_RE = re.compile(r"^[a-z][a-z0-9_]{0,31}$")
ENVIRONMENTS = ("dev", "test", "staging", "prod")
ROLES = ("all", "api", "worker")


class UnsafeConfiguration(RuntimeError):
    """Raised at startup when the environment would run an unsafe configuration (fail closed)."""


def runtime_env() -> str:
    env = os.environ.get("ENV", "dev").strip().lower() or "dev"
    if env not in ENVIRONMENTS:
        raise UnsafeConfiguration(f"ENV must be one of {ENVIRONMENTS}, got {env!r}")
    return env


def chaos_requested() -> bool:
    return os.environ.get("CHAOS_ENABLED", "").strip() == "1"


class ProviderConfig(BaseModel):
    name: str = ""
    litellm_model: str
    api_base_env: str | None = None
    api_base_default: str | None = None
    api_key_env: str | None = None
    enabled_env: str | None = None
    paid: bool = False
    price_in_per_m: float = 0.0
    price_out_per_m: float = 0.0
    timeout_s: float = 20.0
    latency_budget_ms: float = 5000.0
    min_max_tokens: int = 1
    extra: dict[str, Any] = Field(default_factory=dict)

    def enabled(self) -> bool:
        if self.enabled_env is not None:
            flag = os.environ.get(self.enabled_env, "").strip()
            if not flag or flag == "0":
                return False
        return not (self.api_key_env and not os.environ.get(self.api_key_env))

    def api_key(self) -> str | None:
        return os.environ.get(self.api_key_env) if self.api_key_env else None

    def api_base(self) -> str | None:
        """Direct upstream, or the chaos proxy when CHAOS_ENABLED=1 (never in prod, whatever the env says)."""
        base = self.direct_base()
        proxy = os.environ.get("CHAOS_PROXY_URL", "").rstrip("/")
        if chaos_requested() and proxy and base and os.environ.get("ENV", "dev").strip().lower() != "prod":
            return f"{proxy}/{self.name}"
        return base

    def direct_base(self) -> str | None:
        if self.api_base_env and os.environ.get(self.api_base_env):
            return os.environ[self.api_base_env]
        return self.api_base_default

    def cost_usd(self, input_tokens: int, output_tokens: int) -> float:
        return (input_tokens * self.price_in_per_m + output_tokens * self.price_out_per_m) / 1_000_000


class RequestClass(BaseModel):
    name: str = ""
    mode: Literal["interactive", "deferrable"]
    preference: list[str]
    max_tokens_cap: int = 128
    hedge_after_ms: int | None = None


class BreakerPolicy(BaseModel):
    window_s: float = 30
    min_samples: int = 4
    error_rate_threshold: float = 0.5
    open_cooldown_s: float = 10
    max_cooldown_s: float = 60
    half_open_probe_ratio: float = 0.2
    half_open_successes: int = 3
    # A probe lease expires after this long, so an instance that dies mid-probe cannot hold a slot forever.
    probe_lease_s: float = 60


class HedgePolicy(BaseModel):
    max_hedges_per_minute: int = 30


class QueuePolicy(BaseModel):
    base_delay_s: float = 1.0
    max_delay_s: float = 30.0
    max_attempts: int = 8
    poll_interval_s: float = 0.5


class Limits(BaseModel):
    max_prompt_chars: int = 8000
    max_messages: int = 32
    max_features_per_tenant: int = 50


class GatewayConfig(BaseModel):
    providers: dict[str, ProviderConfig]
    classes: dict[str, RequestClass]
    breaker: BreakerPolicy = BreakerPolicy()
    hedging: HedgePolicy = HedgePolicy()
    queue: QueuePolicy = QueuePolicy()
    limits: Limits = Limits()

    def model_post_init(self, __context: Any) -> None:
        for name, p in self.providers.items():
            if not PROVIDER_NAME_RE.match(name):
                raise ValueError(f"provider name {name!r} must match {PROVIDER_NAME_RE.pattern}")
            p.name = name
        for name, c in self.classes.items():
            c.name = name
            unknown = [p for p in c.preference if p not in self.providers]
            if unknown:
                raise ValueError(f"class {name} references unknown providers {unknown}")

    def enabled_providers(self) -> list[str]:
        return [n for n, p in self.providers.items() if p.enabled()]


def load_secrets() -> None:
    """Local dev only: load provider secrets from the shared portfolio env file (never logged).

    Deployed environments (staging/prod) get every value from env vars injected by Cloud Run from Secret
    Manager, so no file is read there.
    """
    if runtime_env() in ("staging", "prod"):
        return
    path = os.environ.get("PORTFOLIO_ENV_FILE", "F:/.secrets/portfolio.env")
    if Path(path).is_file():
        load_dotenv(path, override=False)
    local = REPO_ROOT / ".env"
    if local.is_file():
        load_dotenv(local, override=False)


def load_config(path: Path | None = None) -> GatewayConfig:
    raw = yaml.safe_load((path or Path(os.environ.get("GATEWAY_CONFIG", DEFAULT_CONFIG))).read_text("utf-8"))
    return GatewayConfig.model_validate(raw)


@lru_cache(maxsize=1)
def get_config() -> GatewayConfig:
    return load_config()


def _flag(name: str, default: bool) -> bool:
    v = os.environ.get(name)
    if v is None or not v.strip():
        return default
    return v.strip().lower() in ("1", "true", "yes", "on")


class Settings(BaseModel):
    env: str = "dev"
    role: Literal["all", "api", "worker"] = "all"
    redis_url: str = "redis://127.0.0.1:16380/0"
    redis_password: str | None = None
    redis_ca_pem: str | None = None
    admin_token: str = ""
    project: str = "p3-gateway"
    project_cap_usd: float = 1.0
    global_cap_usd: float = 25.0
    ledger_path: str = "F:/.llm-spend/ledger.jsonl"
    spend_backend: Literal["file", "redis"] = "file"
    request_log_path: str | None = None
    kill_switch: bool = False
    metrics_port: int | None = None
    metrics_bind: str = "127.0.0.1"
    tenant_keys_json: str | None = None
    allow_admin_tenant_create: bool = True
    shutdown_grace_s: float = 8.0

    @classmethod
    def from_env(cls) -> Settings:
        env = runtime_env()
        role = os.environ.get("GATEWAY_ROLE", "all").strip().lower() or "all"
        if role not in ROLES:
            raise UnsafeConfiguration(f"GATEWAY_ROLE must be one of {ROLES}, got {role!r}")
        mp = os.environ.get("METRICS_PORT", "").strip()
        backend = os.environ.get("SPEND_BACKEND", "file").strip().lower()
        if backend not in ("file", "redis"):
            raise UnsafeConfiguration("SPEND_BACKEND must be 'file' or 'redis'")
        return cls(
            env=env,
            role=role,
            redis_url=os.environ.get("REDIS_URL", "redis://127.0.0.1:16380/0"),
            redis_password=os.environ.get("REDIS_PASSWORD") or None,
            redis_ca_pem=os.environ.get("REDIS_CA_CERT_PEM") or None,
            admin_token=os.environ.get("GATEWAY_ADMIN_TOKEN", ""),
            project_cap_usd=float(os.environ.get("PROJECT_SPEND_CAP_USD", "1.0")),
            global_cap_usd=float(os.environ.get("LLM_SPEND_CAP_USD", "25")),
            ledger_path=os.environ.get("LLM_SPEND_LEDGER", "F:/.llm-spend/ledger.jsonl"),
            spend_backend=backend,
            request_log_path=os.environ.get("REQUEST_LOG_PATH") or None,
            kill_switch=_flag("LLM_KILL_SWITCH", False),
            metrics_port=int(mp) if mp else None,
            metrics_bind=os.environ.get("METRICS_BIND", "127.0.0.1"),
            tenant_keys_json=os.environ.get("TENANT_KEYS_JSON") or None,
            allow_admin_tenant_create=_flag("ALLOW_ADMIN_TENANT_CREATE", env != "prod"),
            shutdown_grace_s=float(os.environ.get("SHUTDOWN_GRACE_S", "8")),
        )


def _is_local_host(url: str | None) -> bool:
    if not url:
        return False
    host = (urlparse(url).hostname or "").lower()
    if host in ("localhost", "host.docker.internal") or host.endswith(".local"):
        return True
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return False
    return ip.is_loopback or ip.is_private or ip.is_link_local


def check_runtime_safety(cfg: GatewayConfig, settings: Settings) -> list[str]:
    """Fail closed on configurations that must never run in prod. Returns non-fatal warnings."""
    warnings: list[str] = []
    env = runtime_env()
    if env != "prod":
        return warnings
    if chaos_requested():
        raise UnsafeConfiguration("CHAOS_ENABLED=1 is refused when ENV=prod (fault injection is dev-only)")
    if os.environ.get("CHAOS_PROXY_URL", "").strip():
        raise UnsafeConfiguration("CHAOS_PROXY_URL must not be set when ENV=prod")
    if settings.role in ("all", "api") and len(settings.admin_token) < 32:
        raise UnsafeConfiguration("GATEWAY_ADMIN_TOKEN must be set (>= 32 chars) when ENV=prod")
    if settings.spend_backend != "redis":
        raise UnsafeConfiguration(
            "SPEND_BACKEND=redis is required when ENV=prod (file ledger is per instance)"
        )
    if not settings.redis_url.startswith("rediss://"):
        raise UnsafeConfiguration("REDIS_URL must use TLS (rediss://) when ENV=prod")
    local = [n for n in cfg.enabled_providers() if _is_local_host(cfg.providers[n].direct_base())]
    if local:
        raise UnsafeConfiguration(f"local/private-network providers are dev-only, refused in prod: {local}")
    enabled = cfg.enabled_providers()
    if not enabled:
        raise UnsafeConfiguration("no provider is enabled (check provider secrets)")
    if len(enabled) < 2:
        warnings.append(f"only one provider enabled ({enabled}): no independent failover target")
    return warnings
