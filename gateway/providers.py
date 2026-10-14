"""Adaptadores de provedor.

Contrato: `complete(request)` devolve `ProviderSuccess` ou `ProviderFailure`. Falhas de
rede/HTTP nunca viram exceção para o router. Nada aqui registra prompt, resposta ou chave.
"""

import asyncio
import logging
import os
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from enum import Enum
from typing import Any

import httpx

from gateway.budget import Budget
from gateway.config import ProviderConfig
from gateway.secrets import mask_secrets

log = logging.getLogger("gateway.providers")

CHAT_PATH = "/v1/chat/completions"
# Host FIXO no codigo: nada vindo do request ou do YAML muda para onde a chave e enviada.
ANTHROPIC_URL = "https://api.anthropic.com/v1/messages"
ANTHROPIC_VERSION = "2023-06-01"


class FailureReason(str, Enum):
    RATE_LIMITED = "rate_limited"
    SERVER_ERROR = "server_error"
    TIMEOUT = "timeout"
    CONNECTION_ERROR = "connection_error"
    AUTH_ERROR = "auth_error"  # provedor mal configurado: faz fallback e loga aviso
    INVALID_RESPONSE = "invalid_response"  # 2xx sem JSON válido, ou status inesperado (3xx)
    REAL_API_BLOCKED = "real_api_blocked"  # AnthropicProvider fail-closed
    BUDGET_EXHAUSTED = "budget_exhausted"  # sem reserva no budget (ou budget invalido)
    INPUT_TOO_LARGE = "input_too_large"  # acima de real_api_max_input_tokens
    CLIENT_ERROR = "client_error"  # 4xx do cliente: NÃO faz fallback
    # Usados só pelo router (o provedor não chega a ser chamado):
    CONTEXT_TOO_LARGE = "context_too_large"
    DISABLED = "disabled"
    RATE_LIMIT_LOCAL = "rate_limit_local"  # token bucket local esgotado
    BREAKER_OPEN = "breaker_open"
    COOLING_DOWN = "cooling_down"  # 429 recente: aguardando o Retry-After


# Falhas locais: o provedor nao chegou a abrir conexao. Nao contam para o breaker nem para a
# latencia.
NOT_CALLED = frozenset(
    {FailureReason.REAL_API_BLOCKED, FailureReason.BUDGET_EXHAUSTED, FailureReason.INPUT_TOO_LARGE}
)


def estimate_tokens(request: dict[str, Any]) -> int:
    """APROXIMACAO: ~4 caracteres por token, so sobre o texto das mensagens. Nao e um
    tokenizer real; serve apenas para decidir se o prompt claramente nao cabe."""
    chars = 0
    for msg in request.get("messages", []):
        content = msg.get("content") if isinstance(msg, dict) else None
        if isinstance(content, str):
            chars += len(content)
        elif isinstance(content, list):
            chars += sum(len(p.get("text", "")) for p in content if isinstance(p, dict))
    return chars // 4 + 1


@dataclass(frozen=True)
class ProviderSuccess:
    data: dict[str, Any]
    latency_ms: float


@dataclass(frozen=True)
class ProviderFailure:
    reason: FailureReason
    status_code: int | None = None
    retry_after_s: float | None = None  # já parseado; alimenta o cooldown do router
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
        return await self._post(url, payload, self._headers())

    async def _post(self, url: str, payload: dict[str, Any], headers: dict[str, str]) -> ProviderResult:
        start = time.perf_counter()

        def elapsed() -> float:
            return (time.perf_counter() - start) * 1000

        try:
            # O timeout do httpx vale por fase (conexão, leitura...). O asyncio.timeout é o
            # prazo TOTAL: ao estourar, cancela a requisição de verdade (sem tarefa pendurada).
            async with asyncio.timeout(self.cfg.timeout_s):
                resp = await self._client.post(
                    url, json=payload, headers=headers, timeout=self.cfg.timeout_s
                )
        except (httpx.TimeoutException, TimeoutError):
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
                body=mask_secrets(resp.text).encode("utf-8"),
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
        adapted = self._adapt(data)
        if adapted is None:
            return fail(FailureReason.INVALID_RESPONSE)
        return ProviderSuccess(adapted, latency_ms)

    def _adapt(self, data: dict[str, Any]) -> dict[str, Any] | None:
        """Converte o JSON 200 do upstream para o formato OpenAI (None = invalido)."""
        data.setdefault("model", self.cfg.model)  # o modelo realmente usado vem do upstream
        return data


class MockProvider(OpenAICompatProvider):
    pass


class OllamaProvider(OpenAICompatProvider):
    """Ollama via endpoint compatível com OpenAI (`/v1/chat/completions`)."""


def _text_of(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(p.get("text", "") for p in content if isinstance(p, dict))
    return ""


def to_anthropic_request(request: dict[str, Any], model: str, max_tokens_cap: int) -> dict[str, Any]:
    """OpenAI -> Anthropic. `system` vira o campo `system`; `model` vem do config; `max_tokens`
    e limitado ao cap (e usa o cap quando o request nao informa)."""
    system: list[str] = []
    messages: list[dict[str, str]] = []
    for msg in request.get("messages", []):
        text = _text_of(msg.get("content"))
        if msg.get("role") == "system":
            system.append(text)
        else:
            role = "assistant" if msg.get("role") == "assistant" else "user"
            messages.append({"role": role, "content": text})
    requested = request.get("max_tokens", request.get("max_completion_tokens"))
    if isinstance(requested, bool) or not isinstance(requested, int) or requested <= 0:
        requested = max_tokens_cap
    body: dict[str, Any] = {
        "model": model,
        "max_tokens": min(requested, max_tokens_cap),
        "messages": messages,
    }
    if system:
        body["system"] = "\n\n".join(system)
    temperature = request.get("temperature")
    if isinstance(temperature, (int, float)) and not isinstance(temperature, bool):
        body["temperature"] = min(max(float(temperature), 0.0), 1.0)
    return body


_STOP_REASONS = {"end_turn": "stop", "stop_sequence": "stop", "max_tokens": "length"}


def from_anthropic_response(data: dict[str, Any], model: str) -> dict[str, Any] | None:
    """Anthropic -> OpenAI (None se o formato for inesperado)."""
    content = data.get("content")
    if not isinstance(content, list):
        return None
    text = "".join(b.get("text", "") for b in content if isinstance(b, dict) and b.get("type") == "text")
    usage = data.get("usage") if isinstance(data.get("usage"), dict) else {}
    prompt = usage.get("input_tokens") if isinstance(usage.get("input_tokens"), int) else 0
    completion = usage.get("output_tokens") if isinstance(usage.get("output_tokens"), int) else 0
    return {
        "id": data.get("id") if isinstance(data.get("id"), str) else "msg",
        "object": "chat.completion",
        "model": data.get("model") if isinstance(data.get("model"), str) else model,
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": text},
                "finish_reason": _STOP_REASONS.get(data.get("stop_reason"), "stop"),
            }
        ],
        "usage": {
            "prompt_tokens": prompt,
            "completion_tokens": completion,
            "total_tokens": prompt + completion,
        },
    }


class AnthropicProvider(OpenAICompatProvider):
    """Chamada real a Anthropic, FAIL-CLOSED. So abre conexao se TODAS forem verdadeiras:
    ENABLE_REAL_API=1, ANTHROPIC_API_KEY presente, provedor `enabled`, entrada estimada dentro
    de `real_api_max_input_tokens` e `budget.reserve()` com sucesso (a reserva e a ultima
    checagem, para um bloqueio anterior nao gastar orcamento). Nenhuma falha local conta para o
    breaker. A chave e lida do ambiente so aqui, no momento do uso, e nunca e logada."""

    def __init__(
        self, cfg: ProviderConfig, client: httpx.AsyncClient, budget: Budget | None, on_real_call=None
    ) -> None:
        super().__init__(cfg, client)
        self._budget = budget
        self._on_real_call = on_real_call  # callback sem argumentos (metrica)

    @staticmethod
    def real_api_enabled() -> bool:
        return os.environ.get("ENABLE_REAL_API", "0").strip() == "1"

    def _block(self, reason: FailureReason) -> ProviderFailure:
        log.warning("provider=%s %s", self.name, reason.value)
        return ProviderFailure(reason)

    async def complete(self, request: dict[str, Any]) -> ProviderResult:
        if not self.real_api_enabled():
            return self._block(FailureReason.REAL_API_BLOCKED)
        key = os.environ.get("ANTHROPIC_API_KEY")
        if not key or not self.cfg.enabled:
            return self._block(FailureReason.REAL_API_BLOCKED)
        if estimate_tokens(request) > self.cfg.real_api_max_input_tokens:
            return self._block(FailureReason.INPUT_TOO_LARGE)
        # A reserva precede o envio e nunca e devolvida (falha e timeout tambem contam).
        if self._budget is None or not await asyncio.to_thread(self._budget.reserve):
            return self._block(FailureReason.BUDGET_EXHAUSTED)
        if self._on_real_call:
            self._on_real_call()
        body = to_anthropic_request(request, self.cfg.model, self.cfg.max_tokens_cap)
        headers = {"x-api-key": key, "anthropic-version": ANTHROPIC_VERSION}
        return await self._post(ANTHROPIC_URL, body, headers)

    def _adapt(self, data: dict[str, Any]) -> dict[str, Any] | None:
        return from_anthropic_response(data, self.cfg.model)


def build_providers(
    configs: list[ProviderConfig],
    client: httpx.AsyncClient,
    budget: Budget | None = None,
    on_real_call=None,
) -> dict[str, Provider]:
    providers: dict[str, Provider] = {}
    for cfg in configs:
        if cfg.type == "mock":
            providers[cfg.name] = MockProvider(cfg, client)
        elif cfg.type == "ollama":
            providers[cfg.name] = OllamaProvider(cfg, client)
        else:
            providers[cfg.name] = AnthropicProvider(cfg, client, budget, on_real_call)
    return providers
