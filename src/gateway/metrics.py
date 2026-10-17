"""Prometheus instrumentation. One registry per app instance so tests stay isolated."""

from __future__ import annotations

from prometheus_client import CollectorRegistry, Counter, Gauge, Histogram

LATENCY_BUCKETS = (0.05, 0.1, 0.25, 0.5, 0.75, 1.0, 1.5, 2.0, 3.0, 4.0, 6.0, 8.0, 12.0, 20.0, 30.0)
BREAKER_STATE_VALUE = {"closed": 0, "half_open": 1, "open": 2}


class Metrics:
    def __init__(self, registry: CollectorRegistry | None = None) -> None:
        self.registry = registry or CollectorRegistry()
        r = self.registry
        self.client_requests = Counter(
            "gateway_client_requests_total",
            "Requests answered to clients",
            ["request_class", "status", "served_by"],
            registry=r,
        )
        self.client_latency = Histogram(
            "gateway_client_request_seconds",
            "Client-observed gateway latency",
            ["request_class"],
            buckets=LATENCY_BUCKETS,
            registry=r,
        )
        self.provider_attempts = Counter(
            "gateway_provider_attempts_total",
            "Provider attempts by outcome",
            ["provider", "outcome"],
            registry=r,
        )
        self.provider_errors = Counter(
            "gateway_provider_errors_total",
            "Provider errors by taxonomy",
            ["provider", "error_type"],
            registry=r,
        )
        self.provider_latency = Histogram(
            "gateway_provider_latency_seconds",
            "Successful provider call latency",
            ["provider"],
            buckets=LATENCY_BUCKETS,
            registry=r,
        )
        self.breaker_state = Gauge(
            "gateway_circuit_state", "Circuit state: 0 closed, 1 half-open, 2 open", ["provider"], registry=r
        )
        self.breaker_transitions = Counter(
            "gateway_circuit_transitions_total",
            "Breaker transitions",
            ["provider", "from_state", "to_state"],
            registry=r,
        )
        self.failovers = Counter(
            "gateway_failovers_total",
            "Requests moved off a provider",
            ["request_class", "from_provider", "to_provider", "reason"],
            registry=r,
        )
        self.hedges = Counter("gateway_hedges_total", "Hedged requests by outcome", ["outcome"], registry=r)
        self.queue_depth = Gauge(
            "gateway_queue_depth", "Deferrable jobs waiting in the Redis queue", registry=r
        )
        self.queue_events = Counter(
            "gateway_queue_events_total", "Deferrable queue events", ["event"], registry=r
        )
        self.cost = Counter(
            "gateway_cost_usd_total",
            "Attributed cost in USD (list-price estimate)",
            ["tenant", "feature", "provider"],
            registry=r,
        )
        self.tokens = Counter(
            "gateway_tokens_total",
            "Tokens by tenant/feature/provider",
            ["tenant", "feature", "provider", "direction"],
            registry=r,
        )
        self.idempotent_replays = Counter(
            "gateway_idempotent_replays_total",
            "Duplicate request_ids served from the idempotency store",
            registry=r,
        )
        self.rate_limited = Counter(
            "gateway_tenant_rate_limited_total",
            "Requests rejected by tenant rate limit",
            ["tenant"],
            registry=r,
        )
        self.health_success_rate = Gauge(
            "gateway_health_success_rate", "Sliding-window success rate", ["provider"], registry=r
        )
        self.health_latency = Gauge(
            "gateway_health_latency_seconds",
            "Sliding-window latency percentile",
            ["provider", "quantile"],
            registry=r,
        )
        self.health_samples = Gauge(
            "gateway_health_window_samples", "Samples in the sliding window", ["provider"], registry=r
        )
