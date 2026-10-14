"""Métricas Prometheus. Um `CollectorRegistry` PRÓPRIO por app (nunca o global), então criar
vários apps (como nos testes) não causa registro duplicado.

Labels de baixa cardinalidade: só nomes de provedor (do config), status HTTP, e motivos de
falha (enum). Nunca request_id, prompt ou modelo livre. Nenhuma métrica expõe segredos.
"""

from collections.abc import Callable, Iterable

from prometheus_client import CollectorRegistry, Counter, Histogram, generate_latest
from prometheus_client.core import GaugeMetricFamily
from prometheus_client.registry import Collector

CONTENT_TYPE = "text/plain; version=0.0.4; charset=utf-8"
BREAKER_STATE_VALUE = {"closed": 0, "half_open": 1, "open": 2}
NO_PROVIDER = "none"
LATENCY_BUCKETS = (0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10, 30, 60, 120)


class _ScrapeCollector(Collector):
    """Gauges calculados NO MOMENTO do scrape: o estado do breaker reflete a transição por
    tempo (open -> half_open) e o budget é lido do arquivo."""

    def __init__(
        self,
        breaker_states: Callable[[], dict[str, str]],
        budget_remaining: Callable[[], int],
    ) -> None:
        self._breaker_states = breaker_states
        self._budget_remaining = budget_remaining

    def collect(self) -> Iterable[GaugeMetricFamily]:
        breaker = GaugeMetricFamily(
            "gateway_breaker_state",
            "Estado do circuit breaker (0 closed, 1 half_open, 2 open)",
            labels=["provider"],
        )
        for provider, state in self._breaker_states().items():
            breaker.add_metric([provider], BREAKER_STATE_VALUE[state])
        yield breaker
        remaining = GaugeMetricFamily(
            "gateway_budget_remaining", "Chamadas reais restantes no budget"
        )
        remaining.add_metric([], self._budget_remaining())
        yield remaining


class Metrics:
    def __init__(
        self,
        breaker_states: Callable[[], dict[str, str]],
        budget_remaining: Callable[[], int],
    ) -> None:
        self.registry = CollectorRegistry()
        self.requests = Counter(
            "gateway_requests",
            "Requisições por provedor que respondeu (none no 503) e status HTTP",
            ["provider", "status"],
            registry=self.registry,
        )
        self.fallbacks = Counter(
            "gateway_fallback",
            "Provedores pulados ou que falharam; to_provider é quem acabou respondendo (none no 503)",
            ["from_provider", "to_provider", "reason"],
            registry=self.registry,
        )
        self.latency = Histogram(
            "gateway_latency_seconds",
            "Latência de chamadas efetivamente feitas ao provedor",
            ["provider"],
            buckets=LATENCY_BUCKETS,
            registry=self.registry,
        )
        self.real_api_calls = Counter(
            "gateway_real_api_calls",
            "Chamadas reais à API (budget reservado)",
            registry=self.registry,
        )
        self.registry.register(_ScrapeCollector(breaker_states, budget_remaining))

    def observe_request(self, provider: str, status: int, attempts: list[tuple[str, str]]) -> None:
        self.requests.labels(provider, str(status)).inc()
        for from_provider, reason in attempts:
            self.fallbacks.labels(from_provider, provider, reason).inc()

    def render(self) -> bytes:
        return generate_latest(self.registry)
