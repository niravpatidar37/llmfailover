"""Configuration loading: providers, request classes and policies from config/gateway.yaml plus env."""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path
from typing import Any, Literal

import yaml
from dotenv import load_dotenv
from pydantic import BaseModel, Field

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CONFIG = REPO_ROOT / "config" / "gateway.yaml"


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
        """Direct upstream, or the chaos proxy when CHAOS_ENABLED=1 (proxy prefixes by provider name)."""
        base = self.direct_base()
        proxy = os.environ.get("CHAOS_PROXY_URL", "").rstrip("/")
        if os.environ.get("CHAOS_ENABLED") == "1" and proxy and base:
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
            p.name = name
        for name, c in self.classes.items():
            c.name = name
            unknown = [p for p in c.preference if p not in self.providers]
            if unknown:
                raise ValueError(f"class {name} references unknown providers {unknown}")

    def enabled_providers(self) -> list[str]:
        return [n for n, p in self.providers.items() if p.enabled()]


def load_secrets() -> None:
    """Load provider secrets from the shared portfolio env file (never logged)."""
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


class Settings(BaseModel):
    redis_url: str = "redis://127.0.0.1:16380/0"
    admin_token: str = ""
    project: str = "p3-gateway"
    project_cap_usd: float = 1.0
    global_cap_usd: float = 25.0
    ledger_path: str = "F:/.llm-spend/ledger.jsonl"
    request_log_path: str | None = None

    @classmethod
    def from_env(cls) -> Settings:
        return cls(
            redis_url=os.environ.get("REDIS_URL", "redis://127.0.0.1:16380/0"),
            admin_token=os.environ.get("GATEWAY_ADMIN_TOKEN", ""),
            project_cap_usd=float(os.environ.get("PROJECT_SPEND_CAP_USD", "1.0")),
            global_cap_usd=float(os.environ.get("LLM_SPEND_CAP_USD", "25")),
            ledger_path=os.environ.get("LLM_SPEND_LEDGER", "F:/.llm-spend/ledger.jsonl"),
            request_log_path=os.environ.get("REQUEST_LOG_PATH") or None,
        )
