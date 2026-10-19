"""Deployment controls: prod refuses chaos and local providers, kill switch, liveness vs readiness,
migrations, Secret Manager tenant keys, process roles and JSON logs."""

from __future__ import annotations

import json
import logging
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any, Literal

import fakeredis
import httpx
import pytest

from gateway import migrate
from gateway.app import create_app
from gateway.chaos import ChaosDisabled, create_chaos_app
from gateway.config import (
    DEFAULT_CONFIG,
    REPO_ROOT,
    GatewayConfig,
    Settings,
    UnsafeConfiguration,
    check_runtime_safety,
    load_config,
)
from gateway.logging_setup import JsonFormatter
from gateway.migrations import MIGRATIONS, REQUIRED_VERSION
from gateway.spend import SpendLedger
from gateway.store import hash_key

from .conftest import ADMIN, FakeCaller, chat_body, make_config, new_tenant

PROD_CONFIG = REPO_ROOT / "config" / "gateway.prod.yaml"
PROD_ADMIN = "p" * 40


def prod_settings(**kw: Any) -> Settings:
    base: dict[str, Any] = {
        "env": "prod",
        "redis_url": "rediss://10.0.0.3:6378/0",
        "admin_token": PROD_ADMIN,
        "spend_backend": "redis",
        "allow_admin_tenant_create": False,
    }
    base.update(kw)
    return Settings(**base)


@pytest.fixture
def prod_env(monkeypatch: pytest.MonkeyPatch) -> pytest.MonkeyPatch:
    for k in (
        "CHAOS_ENABLED",
        "CHAOS_PROXY_URL",
        "HF_ENABLED",
        "ANTHROPIC_API_KEY",
        "ANTHROPIC_MODEL",
        "FAKE_LLM",
    ):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("ENV", "prod")
    monkeypatch.setenv("GROQ_API_KEY", "test-not-a-real-key")
    return monkeypatch


def build_prod_app(redis: Any, caller: FakeCaller, **kw: Any) -> Any:
    return create_app(
        cfg=load_config(PROD_CONFIG),
        settings=prod_settings(**kw),
        redis=redis,
        caller=caller,
        start_background=False,
    )


# ---------------------------------------------------------------- chaos can never run in prod


def test_prod_settings_start_without_chaos(prod_env: pytest.MonkeyPatch) -> None:
    """Guard: the prod settings used below are otherwise valid, so the refusals are about chaos."""
    app = build_prod_app(fakeredis.FakeAsyncRedis(), FakeCaller())
    assert app.state.settings.env == "prod"


def test_gateway_refuses_to_start_with_chaos_enabled_in_prod(prod_env: pytest.MonkeyPatch) -> None:
    prod_env.setenv("CHAOS_ENABLED", "1")
    with pytest.raises(UnsafeConfiguration, match="CHAOS_ENABLED"):
        build_prod_app(fakeredis.FakeAsyncRedis(), FakeCaller())


def test_gateway_refuses_chaos_proxy_url_in_prod(prod_env: pytest.MonkeyPatch) -> None:
    prod_env.setenv("CHAOS_PROXY_URL", "http://127.0.0.1:8113")
    with pytest.raises(UnsafeConfiguration, match="CHAOS_PROXY_URL"):
        build_prod_app(fakeredis.FakeAsyncRedis(), FakeCaller())


def test_chaos_proxy_refuses_to_start_in_prod_even_when_enabled(prod_env: pytest.MonkeyPatch) -> None:
    prod_env.setenv("CHAOS_ENABLED", "1")
    with pytest.raises(ChaosDisabled, match="prod"):
        create_chaos_app(admin_token="x" * 32)


def test_provider_never_routes_through_chaos_proxy_in_prod(monkeypatch: pytest.MonkeyPatch) -> None:
    groq = load_config(PROD_CONFIG).providers["groq"]
    monkeypatch.setenv("CHAOS_ENABLED", "1")
    monkeypatch.setenv("CHAOS_PROXY_URL", "http://127.0.0.1:8113")
    monkeypatch.setenv("ENV", "dev")
    assert groq.api_base() == "http://127.0.0.1:8113/groq"  # dev: chaos routing works (guard)
    monkeypatch.setenv("ENV", "prod")
    assert groq.api_base() == "https://api.groq.com/openai/v1"


@pytest.mark.parametrize("env", ["production", "staging2", "x"])
def test_unknown_env_fails_closed(monkeypatch: pytest.MonkeyPatch, env: str) -> None:
    monkeypatch.setenv("ENV", env)
    with pytest.raises(UnsafeConfiguration):
        Settings.from_env()


@pytest.mark.parametrize(
    ("override", "match"),
    [
        ({"admin_token": "short"}, "GATEWAY_ADMIN_TOKEN"),
        ({"spend_backend": "file"}, "SPEND_BACKEND"),
        ({"redis_url": "redis://10.0.0.3:6379/0"}, "TLS"),
    ],
)
def test_prod_refuses_weak_settings(
    prod_env: pytest.MonkeyPatch, override: dict[str, Any], match: str
) -> None:
    with pytest.raises(UnsafeConfiguration, match=match):
        check_runtime_safety(load_config(PROD_CONFIG), prod_settings(**override))


def test_prod_refuses_local_ollama_providers(prod_env: pytest.MonkeyPatch) -> None:
    """The dev config (Ollama on 127.0.0.1) cannot be started in prod by accident."""
    with pytest.raises(UnsafeConfiguration, match="dev-only"):
        check_runtime_safety(load_config(DEFAULT_CONFIG), prod_settings())


# ---------------------------------------------------------------- prod provider set


def test_prod_config_has_only_external_https_providers() -> None:
    cfg = load_config(PROD_CONFIG)
    assert not [n for n in cfg.providers if n.startswith("ollama")]
    for p in cfg.providers.values():
        assert (p.direct_base() or "https://default").startswith("https://") or p.name == "anthropic"
    for c in cfg.classes.values():
        assert c.preference and set(c.preference) <= set(cfg.providers)


def test_prod_with_only_groq_keyed_has_one_provider_and_warns(prod_env: pytest.MonkeyPatch) -> None:
    cfg = load_config(PROD_CONFIG)
    assert cfg.enabled_providers() == ["groq"]
    warnings = check_runtime_safety(cfg, prod_settings())
    assert any("only one provider" in w for w in warnings)
    assert any("fewer than 2" in w for w in warnings)  # startup failover-capacity WARN
    prod_env.setenv("ANTHROPIC_API_KEY", "test-not-a-real-key")
    assert cfg.enabled_providers() == ["groq", "anthropic"]
    # A keyed provider whose model id is still the placeholder is refused, not silently enabled.
    with pytest.raises(UnsafeConfiguration, match="placeholder model id"):
        check_runtime_safety(cfg, prod_settings())
    prod_env.setenv("ANTHROPIC_MODEL", "anthropic/model-id-verified-via-v1-models")
    assert cfg.providers["anthropic"].model() == "anthropic/model-id-verified-via-v1-models"
    assert check_runtime_safety(cfg, prod_settings()) == []


# ---------------------------------------------------------------- kill switch


@pytest.fixture
async def dev_client(
    redis: fakeredis.FakeAsyncRedis, caller: FakeCaller, ledger: SpendLedger, tmp_path: Path
) -> AsyncIterator[httpx.AsyncClient]:
    await migrate.apply(redis)
    st = Settings(redis_url="redis://unused", admin_token=ADMIN, ledger_path=str(tmp_path / "l.jsonl"))
    app = create_app(
        cfg=make_config(), settings=st, redis=redis, caller=caller, ledger=ledger, start_background=False
    )
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://gw") as c:
        c.app = app  # type: ignore[attr-defined]
        yield c


def _auth(key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {key}"}


async def test_kill_switch_env_flag_blocks_every_provider_call(
    redis: fakeredis.FakeAsyncRedis, caller: FakeCaller, tmp_path: Path
) -> None:
    st = Settings(
        redis_url="redis://unused", admin_token=ADMIN, kill_switch=True, ledger_path=str(tmp_path / "l")
    )
    app = create_app(cfg=make_config(), settings=st, redis=redis, caller=caller, start_background=False)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://gw") as c:
        key = await new_tenant(c)
        for cls in ("generate", "batch"):  # interactive AND deferrable: nothing is called or queued
            r = await c.post(
                "/v1/chat/completions",
                json=chat_body(rid=f"kill-{cls}-0001", request_class=cls),
                headers=_auth(key),
            )
            assert r.status_code == 503 and r.json()["error"]["code"] == "kill_switch"
    assert caller.calls == []
    assert await redis.zcard("gw:queue") == 0


async def test_kill_switch_admin_toggle_is_instant_and_reversible(
    dev_client: httpx.AsyncClient, caller: FakeCaller
) -> None:
    key = await new_tenant(dev_client)
    adm = {"X-Admin-Token": ADMIN}
    assert (await dev_client.put("/admin/kill-switch", json={"engaged": True})).status_code == 403
    r = await dev_client.put("/admin/kill-switch", json={"engaged": True}, headers=adm)
    assert r.json()["engaged"] is True
    r = await dev_client.post("/v1/chat/completions", json=chat_body(rid="ks-req-0001"), headers=_auth(key))
    assert r.status_code == 503 and caller.calls == []
    await dev_client.put("/admin/kill-switch", json={"engaged": False}, headers=adm)
    r = await dev_client.post("/v1/chat/completions", json=chat_body(rid="ks-req-0001"), headers=_auth(key))
    assert r.status_code == 200  # the refused request released its idempotency claim, so a retry runs


async def test_worker_leaves_queue_untouched_while_kill_switch_engaged(
    dev_client: httpx.AsyncClient, caller: FakeCaller, redis: fakeredis.FakeAsyncRedis
) -> None:
    svc = dev_client.app.state.service  # type: ignore[attr-defined]
    await redis.zadd("gw:queue", {"acme:job-00000001": 0})
    await dev_client.app.state.kill_switch.set(True)  # type: ignore[attr-defined]
    assert await svc.worker_tick() == 0
    assert await redis.zcard("gw:queue") == 1 and caller.calls == []


# ---------------------------------------------------------------- /healthz vs /readyz, migrations


class BrokenRedis(fakeredis.FakeAsyncRedis):  # type: ignore[misc]
    async def ping(self, **kw: Any) -> Any:
        raise ConnectionError("redis down")


async def test_healthz_is_liveness_only_and_readyz_checks_dependencies(tmp_path: Path) -> None:
    st = Settings(redis_url="redis://unused", admin_token=ADMIN, ledger_path=str(tmp_path / "l"))
    app = create_app(
        cfg=make_config(), settings=st, redis=BrokenRedis(), caller=FakeCaller(), start_background=False
    )
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://gw") as c:
        assert (await c.get("/healthz")).status_code == 200  # a Redis blip must not restart instances
        r = await c.get("/readyz")
        assert r.status_code == 503 and r.json()["redis"] is False


async def test_readyz_requires_migrations_and_fails_while_draining(
    redis: fakeredis.FakeAsyncRedis, tmp_path: Path
) -> None:
    st = Settings(redis_url="redis://unused", admin_token=ADMIN, ledger_path=str(tmp_path / "l"))
    app = create_app(cfg=make_config(), settings=st, redis=redis, caller=FakeCaller(), start_background=False)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://gw") as c:
        r = await c.get("/readyz")
        assert r.status_code == 503 and r.json()["schema"] == {"current": 0, "required": REQUIRED_VERSION}
        await migrate.apply(redis)
        r = await c.get("/readyz")
        assert r.status_code == 200 and r.json()["status"] == "ready"
        app.state.draining = True  # set by the lifespan on SIGTERM
        assert (await c.get("/readyz")).status_code == 503


async def test_migrations_are_versioned_idempotent_and_upgrade_legacy_keys(
    redis: fakeredis.FakeAsyncRedis,
) -> None:
    assert [m.version for m in MIGRATIONS] == list(range(1, REQUIRED_VERSION + 1))
    await redis.hset(
        "gw:breaker:groq",
        mapping={
            "state": "open",
            "changed_at": "1",
            "opened_at": "1",
            "cooldown_s": "10",
            "probe_successes": "0",
        },
    )
    await redis.hset("gw:tenant:acme", mapping={"rpm": 60, "key_hash": "ab" * 32})
    assert await migrate.apply(redis) == list(range(1, REQUIRED_VERSION + 1))
    assert await redis.hget("gw:breaker:groq", "epoch") == b"0"
    assert await redis.smembers("gw:tenant:acme:keys") == {("ab" * 32).encode()}
    assert await migrate.apply(redis) == []  # re-running is a no-op
    assert await migrate.current_version(redis) == REQUIRED_VERSION


async def test_migration_lock_prevents_concurrent_runs(redis: fakeredis.FakeAsyncRedis) -> None:
    await redis.set(migrate.LOCK_KEY, "someone-else", ex=60)
    with pytest.raises(migrate.MigrationLocked):
        await migrate.apply(redis)
    assert await migrate.current_version(redis) == 0


# ---------------------------------------------------------------- tenant keys from Secret Manager


async def test_tenant_keys_from_secret_spec_authenticate_rotate_and_revoke(
    prod_env: pytest.MonkeyPatch, redis: fakeredis.FakeAsyncRedis
) -> None:
    caller = FakeCaller()
    old_key, new_key = "gwk_" + "o" * 43, "gwk_" + "n" * 43
    spec = {"acme": {"rpm": 100, "key_sha256": [hash_key(old_key), hash_key(new_key)]}}
    await migrate.apply(redis)
    app = build_prod_app(redis, caller, tenant_keys_json=json.dumps(spec))
    transport = httpx.ASGITransport(app=app)
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(transport=transport, base_url="http://g") as c,
    ):
        for i, k in enumerate((old_key, new_key)):  # overlap window: both keys work
            r = await c.post("/v1/chat/completions", json=chat_body(rid=f"sm-req-000{i}"), headers=_auth(k))
            assert r.status_code == 200, r.text
        # prod: keys are only managed in Secret Manager, never minted by the admin API
        r = await c.post("/admin/tenants", json={"tenant": "evil"}, headers={"X-Admin-Token": PROD_ADMIN})
        assert r.status_code == 403 and r.json()["error"]["code"] == "managed_by_secret_manager"
    spec["acme"]["key_sha256"] = [hash_key(new_key)]  # rotation complete: old key revoked
    app2 = build_prod_app(redis, caller, tenant_keys_json=json.dumps(spec))
    async with (
        app2.router.lifespan_context(app2),
        httpx.AsyncClient(transport=httpx.ASGITransport(app=app2), base_url="http://g") as c,
    ):
        r = await c.post("/v1/chat/completions", json=chat_body(rid="sm-req-0010"), headers=_auth(old_key))
        assert r.status_code == 401
        r = await c.post("/v1/chat/completions", json=chat_body(rid="sm-req-0011"), headers=_auth(new_key))
        assert r.status_code == 200
    assert await redis.get(f"gw:apikey:{hash_key(old_key)}") is None


async def test_malformed_tenant_spec_fails_startup(prod_env: pytest.MonkeyPatch) -> None:
    app = build_prod_app(
        fakeredis.FakeAsyncRedis(),
        FakeCaller(),
        tenant_keys_json=json.dumps({"acme": {"key_sha256": ["not-a-hash"]}}),
    )
    with pytest.raises(ValueError, match="64 hex"):
        async with app.router.lifespan_context(app):
            pass


# ---------------------------------------------------------------- roles, metrics port, logs


async def test_worker_role_serves_no_api_and_api_role_runs_no_worker(
    redis: fakeredis.FakeAsyncRedis, tmp_path: Path
) -> None:
    cfg: GatewayConfig = make_config()
    roles: tuple[tuple[Literal["worker", "api"], int], ...] = (("worker", 404), ("api", 401))
    for role, api_status in roles:
        st = Settings(
            redis_url="redis://unused", admin_token=ADMIN, role=role, ledger_path=str(tmp_path / "l")
        )
        app = create_app(cfg=cfg, settings=st, redis=redis, caller=FakeCaller(), start_background=False)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://g") as c:
            assert (await c.post("/v1/chat/completions", json=chat_body())).status_code == api_status
            assert (await c.get("/healthz")).status_code == 200


async def test_metrics_leave_the_public_port_when_metrics_port_is_set(
    redis: fakeredis.FakeAsyncRedis, tmp_path: Path
) -> None:
    """Prod serves /metrics only on a localhost port for the GMP sidecar: tenant names and costs stay
    off the public ingress."""
    st = Settings(
        redis_url="redis://unused", admin_token=ADMIN, metrics_port=59999, ledger_path=str(tmp_path / "l")
    )
    app = create_app(cfg=make_config(), settings=st, redis=redis, caller=FakeCaller(), start_background=False)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://g") as c:
        assert (await c.get("/metrics")).status_code == 404


def test_json_log_formatter_emits_structured_redacted_lines() -> None:
    rec = logging.LogRecord(
        "gateway.requests",
        logging.INFO,
        __file__,
        1,
        json.dumps({"tenant": "acme", "prompt_preview": "mail jane.doe@example.com"}),
        None,
        None,
    )
    out = json.loads(JsonFormatter().format(rec))
    assert out["severity"] == "INFO" and out["tenant"] == "acme" and out["logger"] == "gateway.requests"
    assert "jane.doe@example.com" not in json.dumps(out)
    plain = json.loads(
        JsonFormatter().format(
            logging.LogRecord("gateway", logging.WARNING, __file__, 1, "plain %s", ("text",), None)
        )
    )
    assert plain == {**plain, "severity": "WARNING", "message": "plain text"}


def test_redacting_filter_keeps_json_lines_valid_and_numbers_intact() -> None:
    from gateway.redact import RedactingFilter

    line = json.dumps({"ts": 1792221893.65, "prompt_preview": "call +1 (415) 555-0132", "status": 200})
    rec = logging.LogRecord("gateway.requests", logging.INFO, __file__, 1, line, None, None)
    RedactingFilter().filter(rec)
    out = json.loads(rec.getMessage())  # still valid JSON (before the fix: "ts": [REDACTED_PHONE].65)
    assert out["ts"] == 1792221893.65 and out["status"] == 200
    assert "555-0132" not in out["prompt_preview"]
