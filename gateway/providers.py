"""Adaptadores de provedor.

Contrato: `complete(request)` devolve `ProviderSuccess` ou `ProviderFailure`. Falhas de
rede/HTTP nunca viram exceção para o router. Nada aqui registra prompt, resposta ou chave.
"""

import logging
import os
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from enum import Enum
from typing import Any

import httpx

from gateway.config import ProviderConfig

log = logging.getLogger("gateway.providers")

CHAT_PATH = "/v1/chat/completions"


class FailureReason(str, Enum):
    RATE_LIMITED = "rate_limited"
    SERVER_ERROR = "server_error"
    TIMEOUT = "timeout"
    CONNECTION_ERROR = "connection_error"
    AUTH_ERROR = "auth_error"  # provedor mal configurado: faz fallback e loga aviso
    INVALID_RESPONSE = "invalid_response"  # 2xx sem JSON válido, ou status inesperado (3xx)
    REAL_API_BLOCKED = "real_api_blocked"  # AnthropicProvider fail-closed
    CLIENT_ERROR = "client_error"  # 4xx do cliente: NÃO faz fallback
    # Usados só pelo router (o provedor não chega a ser chamado):
    CONTEXT_TOO_LARGE = "context_too_large"
    DISABLED = "disabled"


@dataclass(frozen=True)
class ProviderSuccess:
    data: dict[str, Any]
    latency_ms: float


@dataclass(frozen=True)
class ProviderFailure:
    reason: FailureReason
    status_code: int | None = None
    retry_after_s: float | None = None  # já parseado; o Marco 4 vai usar
    latency_ms: float = 0.0
    # Só para CLIENT_ERROR: a resposta original, devolvida ao cliente como veio.
    body: bytes | None = None
    content_type: str | None = None

    @property
    def triggers_fallback(self) -> bool:
        return self.reason is not FailureReason.CLIENT_ERROR


ProviderResult = ProviderSuccess | ProviderFailure


def parse_retry_after(value: str | None) -> float | None:
    """Retry-After em segundos ou como data HTTP. Devolve segundos >= 0, ou None."""
    if not value:
        return None
    value = value.strip()
    try:
        return max(0.0, float(value))
    except ValueError:
        pass
    try:
        when = parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    return max(0.0, (when - datetime.now(timezone.utc)).total_seconds())


class Provider:
    def __init__(self, cfg: ProviderConfig) -> None:
        self.cfg = cfg

    @property
    def name(self) -> str:
        return self.cfg.name

    async def complete(self, request: dict[str, Any]) -> ProviderResult:
        raise NotImplementedError


class OpenAICompatProvider(Provider):
    """Base para quem fala o formato OpenAI em `/v1/chat/completions` (mock e Ollama)."""

    def __init__(self, cfg: ProviderConfig, client: httpx.AsyncClient) -> None:
        super().__init__(cfg)
        self._client = client  # um único AsyncClient, compartilhado

    def _headers(self) -> dict[str, str]:
        return {}

    async def complete(self, request: dict[str, Any]) -> ProviderResult:
        # O model do cliente é aceito, mas substituído pelo do provedor.
        payload = {**request, "model": self.cfg.model, "stream": False}
        url = (self.cfg.base_url or "").rstrip("/") + CHAT_PATH
        start = time.perf_counter()

        def elapsed() -> float:
            return (time.perf_counter() - start) * 1000

        try:
            resp = await self._client.post(
                url, json=payload, headers=self._headers(), timeout=self.cfg.timeout_s
            )
        except httpx.TimeoutException:
            return ProviderFailure(FailureReason.TIMEOUT, latency_ms=elapsed())
        except httpx.RequestError:
            return ProviderFailure(FailureReason.CONNECTION_ERROR, latency_ms=elapsed())

        return self._classify(resp, elapsed())

    def _classify(self, resp: httpx.Response, latency_ms: float) -> ProviderResult:
        status = resp.status_code

        def fail(reason: FailureReason, **extra: Any) -> ProviderFailure:
            return ProviderFailure(reason, status, latency_ms=latency_ms, **extra)

        if status == 429:
            return fail(
                FailureReason.RATE_LIMITED,
                retry_after_s=parse_retry_after(resp.headers.get("retry-after")),
            )
        if status >= 500:
            return fail(FailureReason.SERVER_ERROR)
        if status in (401, 403):
            log.warning("provider=%s status=%s auth_error (verifique a configuração)", self.name, status)
            return fail(FailureReason.AUTH_ERROR)
        if 400 <= status < 500:
            return fail(
                FailureReason.CLIENT_ERROR,
                body=resp.content,
                content_type=resp.headers.get("content-type"),
            )
        if status != 200:
            return fail(FailureReason.INVALID_RESPONSE)
        try:
            data = resp.json()
        except ValueError:
            return fail(FailureReason.INVALID_RESPONSE)
        if not isinstance(data, dict):
            return fail(FailureReason.INVALID_RESPONSE)
        data.setdefault("model", self.cfg.model)  # o modelo realmente usado vem do upstream
        return ProviderSuccess(data, latency_ms)


class MockProvider(OpenAICompatProvider):
    pass


class OllamaProvider(OpenAICompatProvider):
    """Ollama via endpoint compatível com OpenAI (`/v1/chat/completions`)."""


class AnthropicProvider(Provider):
    """FAIL-CLOSED: neste marco recusa qualquer chamada real, mesmo com ENABLE_REAL_API=1 e
    chave presente. Não há nenhuma chamada HTTP aqui. A liberação só virá quando o budget
    (Marco 5) existir: reservar a chamada antes de enviar, respeitar o limite, e só então
    montar o request (chave lida de os.environ["ANTHROPIC_API_KEY"], nunca do YAML).
    """

    async def complete(self, request: dict[str, Any]) -> ProviderResult:
        log.warning("provider=%s real_api_blocked", self.name)
        return ProviderFailure(FailureReason.REAL_API_BLOCKED)

    @staticmethod
    def _api_key() -> str | None:  # ponto de extensão do Marco 5; ainda sem uso
        return os.environ.get("ANTHROPIC_API_KEY")


def build_providers(
    configs: list[ProviderConfig], client: httpx.AsyncClient
) -> dict[str, Provider]:
    providers: dict[str, Provider] = {}
    for cfg in configs:
        if cfg.type == "mock":
            providers[cfg.name] = MockProvider(cfg, client)
        elif cfg.type == "ollama":
            providers[cfg.name] = OllamaProvider(cfg, client)
        else:
            providers[cfg.name] = AnthropicProvider(cfg)
    return providers
