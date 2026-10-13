"""Gateway HTTP (formato OpenAI) com fallback entre provedores.

    python -m gateway.main

Logs de aplicação só têm request_id, provedor, motivo e latência: nunca prompt, resposta
ou chaves.
"""

import logging
import math
import time
import uuid
from contextlib import asynccontextmanager
from typing import Any

import httpx
import uvicorn
from dotenv import load_dotenv
from fastapi import FastAPI, Header
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, ConfigDict, Field

from gateway.config import HOST, AppConfig, load_config
from gateway.providers import OllamaProvider, ProviderSuccess, build_providers
from gateway.ratelimit import Clock
from gateway.router import (
    AllProvidersFailed,
    Attempt,
    ClientRequestError,
    Router,
    UnknownProvider,
)

log = logging.getLogger("gateway")


class Message(BaseModel):
    model_config = ConfigDict(extra="allow")

    role: str
    content: str | list[Any] | None = None


class ChatRequest(BaseModel):
    model_config = ConfigDict(extra="allow")

    model: str | None = None  # aceito, mas substituído pelo model do provedor
    messages: list[Message] = Field(min_length=1)
    stream: bool = False


def _error(status: int, message: str, err_type: str, headers: dict[str, str] | None = None, **extra):
    body = {"error": {"message": message, "type": err_type, "code": status, **extra}}
    return JSONResponse(body, status_code=status, headers=headers)


def _fallback_header(attempts: list[Attempt]) -> dict[str, str]:
    if not attempts:
        return {}
    return {"x-gateway-fallback-reason": ",".join(f"{n}:{r.value}" for n, r in attempts)}


async def warmup_providers(router: Router) -> None:
    """Uma requisição mínima (max_tokens=1) aos Ollama com `warmup: true`, para o carregamento
    a frio do modelo não ser confundido com falha. Não passa pelo breaker; falha só avisa."""
    for p in router.providers.values():
        if not (p.cfg.warmup and p.cfg.enabled and isinstance(p, OllamaProvider)):
            continue
        result = await p.complete({"messages": [{"role": "user", "content": "hi"}], "max_tokens": 1})
        if isinstance(result, ProviderSuccess):
            log.info("warmup provider=%s ok latency_ms=%.0f", p.name, result.latency_ms)
        else:
            log.warning("warmup provider=%s falhou reason=%s", p.name, result.reason.value)


def create_app(
    config: AppConfig, client: httpx.AsyncClient | None = None, now: Clock = time.monotonic
) -> FastAPI:
    """`client` permite injetar um AsyncClient (com transports de teste); `now` injeta o
    relógio de bucket, breaker e cooldown. Sem `client`, cria um único AsyncClient
    reutilizado, fechado no shutdown."""
    owns_client = client is None
    http = client or httpx.AsyncClient()
    router = Router(config, build_providers(config.providers, http), now)

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        await warmup_providers(router)
        yield
        if owns_client:
            await http.aclose()

    app = FastAPI(title="llm-gateway", docs_url=None, redoc_url=None, lifespan=lifespan)

    app.state.router = router  # exposto para inspeção em testes

    @app.get("/health")
    async def health():
        return {"status": "ok"}

    @app.get("/status")
    async def status():
        """Somente leitura e sem segredos: estado de resiliência por provedor."""
        return {
            "providers": {
                p.name: {
                    "type": p.cfg.type,
                    "enabled": p.cfg.enabled,
                    **router.guards[p.name].snapshot(),
                }
                for p in router.providers.values()
            }
        }

    @app.post("/v1/chat/completions")
    async def chat(
        req: ChatRequest,
        x_gateway_force_provider: str | None = Header(default=None),
    ):
        if req.stream:
            return _error(
                400, "stream=true não é suportado por este gateway (streaming fora do escopo).",
                "invalid_request_error",
            )
        request_id = uuid.uuid4().hex[:12]
        # O header só vale se force_provider_enabled; caso contrário é ignorado.
        force = x_gateway_force_provider if config.gateway.force_provider_enabled else None
        payload = req.model_dump(exclude_none=True)

        try:
            result = await router.route(payload, request_id, force)
        except UnknownProvider as e:
            return _error(400, f"provedor desconhecido: {e}", "invalid_request_error")
        except ClientRequestError as e:
            headers = {"x-gateway-provider": e.provider, **_fallback_header(e.fallbacks)}
            return Response(
                e.failure.body, status_code=e.failure.status_code or 400,
                media_type=e.failure.content_type, headers=headers,
            )
        except AllProvidersFailed as e:
            headers = _fallback_header(e.attempts)
            if e.retry_after_s is not None:  # inteiro, arredondado para cima
                headers["Retry-After"] = str(max(1, math.ceil(e.retry_after_s)))
            return _error(
                503, "Todos os provedores falharam.", "gateway_error",
                headers=headers,
                providers={n: r.value for n, r in e.attempts},
            )

        headers = {"x-gateway-provider": result.provider, **_fallback_header(result.fallbacks)}
        return JSONResponse(result.data, headers=headers)

    return app


def app_factory() -> FastAPI:
    """Para uvicorn: `uvicorn gateway.main:app_factory --factory --host 127.0.0.1`."""
    load_dotenv()
    return create_app(load_config())


if __name__ == "__main__":
    load_dotenv()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    cfg = load_config()
    uvicorn.run(create_app(cfg), host=HOST, port=cfg.gateway.port)
