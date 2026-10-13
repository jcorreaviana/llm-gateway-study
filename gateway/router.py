"""Router: percorre a chain e cai para o próximo provedor quando o atual falha."""

import logging
from dataclasses import dataclass, field
from typing import Any

from gateway.config import AppConfig
from gateway.providers import (
    FailureReason,
    Provider,
    ProviderFailure,
    ProviderSuccess,
)

log = logging.getLogger("gateway.router")

Attempt = tuple[str, FailureReason]


def estimate_tokens(request: dict[str, Any]) -> int:
    """APROXIMAÇÃO: ~4 caracteres por token, só sobre o texto das mensagens. Não é um
    tokenizer real; serve apenas para decidir se o prompt claramente não cabe."""
    chars = 0
    for msg in request.get("messages", []):
        content = msg.get("content") if isinstance(msg, dict) else None
        if isinstance(content, str):
            chars += len(content)
        elif isinstance(content, list):
            chars += sum(len(p.get("text", "")) for p in content if isinstance(p, dict))
    return chars // 4 + 1


@dataclass
class RouteSuccess:
    data: dict[str, Any]
    provider: str
    fallbacks: list[Attempt] = field(default_factory=list)


class AllProvidersFailed(Exception):
    def __init__(self, attempts: list[Attempt]) -> None:
        super().__init__("all providers failed")
        self.attempts = attempts


class ClientRequestError(Exception):
    """4xx do cliente: devolvido como veio, sem fallback."""

    def __init__(self, provider: str, failure: ProviderFailure, fallbacks: list[Attempt]) -> None:
        super().__init__("client error")
        self.provider = provider
        self.failure = failure
        self.fallbacks = fallbacks


class UnknownProvider(Exception):
    pass


class Router:
    def __init__(self, config: AppConfig, providers: dict[str, Provider]) -> None:
        self.config = config
        self.providers = providers

    async def check_available(self, provider: Provider) -> FailureReason | None:
        """Ponto de extensão do Marco 4: token bucket e circuit breaker entram aqui.
        Devolve o motivo para pular o provedor, ou None se ele pode ser tentado."""
        return None

    def _pre_check(self, provider: Provider, request: dict[str, Any]) -> FailureReason | None:
        if not provider.cfg.enabled:
            return FailureReason.DISABLED
        if estimate_tokens(request) > provider.cfg.context_window:
            return FailureReason.CONTEXT_TOO_LARGE
        return None

    async def route(
        self, request: dict[str, Any], request_id: str, force_provider: str | None = None
    ) -> RouteSuccess:
        if force_provider is not None:
            if force_provider not in self.providers:
                raise UnknownProvider(force_provider)
            order = [force_provider]  # sem fallback
        else:
            order = self.config.chain

        attempts: list[Attempt] = []
        for name in order:
            provider = self.providers[name]
            reason = self._pre_check(provider, request) or await self.check_available(provider)
            if reason is not None:
                log.info("request_id=%s provider=%s skipped reason=%s", request_id, name, reason.value)
                attempts.append((name, reason))
                continue

            result = await provider.complete(request)
            if isinstance(result, ProviderSuccess):
                log.info(
                    "request_id=%s provider=%s ok latency_ms=%.0f", request_id, name, result.latency_ms
                )
                return RouteSuccess(result.data, name, attempts)

            log.info(
                "request_id=%s provider=%s failed reason=%s status=%s latency_ms=%.0f",
                request_id,
                name,
                result.reason.value,
                result.status_code,
                result.latency_ms,
            )
            if not result.triggers_fallback:
                raise ClientRequestError(name, result, attempts)
            attempts.append((name, result.reason))

        raise AllProvidersFailed(attempts)
