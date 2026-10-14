"""Mock de um provedor de LLM de mercado, sabotável em tempo de execução.

Imita `POST /v1/chat/completions` (formato OpenAI) e expõe `/chaos` para injetar
falhas. Não usa modelo real, rede externa, credenciais nem disco. Só escuta em
127.0.0.1.

    python -m mock_upstream.server
"""

import asyncio
import time
import uuid
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any, Literal

import uvicorn
import yaml
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

HOST = "127.0.0.1"  # fixo de propósito: o mock nunca fica exposto na rede
DEFAULT_PORT = 9001
CONFIG_PATH = Path(__file__).resolve().parent.parent / "config.yaml"


class ChaosConfig(BaseModel):
    """Comportamento do mock. POST /chaos substitui a config inteira (campos
    omitidos voltam ao padrão)."""

    model_config = ConfigDict(extra="forbid")

    mode: Literal["normal", "rate_limit", "server_error", "slow"] = "normal"
    retry_after_seconds: int = Field(default=1, ge=0)
    error_status: Literal[500, 503] = 500
    latency_ms: int = Field(default=0, ge=0)
    fail_every_n: int | None = Field(default=None, ge=1)
    fail_status: int = Field(default=500, ge=400, le=599)


class Message(BaseModel):
    model_config = ConfigDict(extra="allow")

    role: str
    content: str | list[Any] | None = None


class ChatRequest(BaseModel):
    model_config = ConfigDict(extra="allow")

    model: str = "mock-market-1"
    messages: list[Message] = Field(min_length=1)


class _State:
    """Estado em memória; todo acesso passa pelo lock."""

    def __init__(self) -> None:
        self.lock = asyncio.Lock()
        self.chaos = ChaosConfig()
        self.chat_requests = 0  # requisições ao chat desde o último /chaos
        self.received = 0
        self.by_status: dict[int, int] = {}


def _error(status: int, message: str, err_type: str, headers: dict[str, str] | None = None):
    body = {"error": {"message": message, "type": err_type, "code": status}}
    return JSONResponse(body, status_code=status, headers=headers)


def _text_of(content: str | list[Any] | None) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):  # partes no estilo OpenAI: [{"type":"text","text":...}]
        return " ".join(p.get("text", "") for p in content if isinstance(p, dict))
    return ""


def _failure(status: int, chaos: ChaosConfig):
    if status == 429:
        return _error(
            429,
            "Rate limit exceeded (mock).",
            "rate_limit_error",
            {"Retry-After": str(chaos.retry_after_seconds)},
        )
    if status >= 500:
        return _error(status, "Upstream failure (mock).", "server_error")
    return _error(status, "Request failed (mock).", "invalid_request_error")


def create_app(sleep: Callable[[float], Awaitable[None]] = asyncio.sleep) -> FastAPI:
    """`sleep` é injetável para os testes medirem o pedido de espera sem depender do relógio."""
    app = FastAPI(title="mock-upstream", docs_url=None, redoc_url=None)
    state = _State()

    async def record(status: int) -> None:
        async with state.lock:
            state.by_status[status] = state.by_status.get(status, 0) + 1

    @app.get("/health")
    async def health():
        return {"status": "ok"}

    @app.get("/chaos")
    async def get_chaos():
        async with state.lock:
            return state.chaos.model_dump()

    @app.post("/chaos")
    async def set_chaos(cfg: ChaosConfig):
        async with state.lock:
            state.chaos = cfg
            state.chat_requests = 0
            return state.chaos.model_dump()

    @app.post("/chaos/reset")
    async def reset_chaos():
        async with state.lock:
            state.chaos = ChaosConfig()
            state.chat_requests = 0
            return state.chaos.model_dump()

    @app.get("/stats")
    async def stats():
        async with state.lock:
            return {
                "received": state.received,
                "by_status": {str(k): v for k, v in sorted(state.by_status.items())},
            }

    @app.post("/stats/reset")
    async def reset_stats():
        async with state.lock:
            state.received = 0
            state.by_status = {}
            return {"received": 0, "by_status": {}}

    @app.post("/v1/chat/completions")
    async def chat(req: ChatRequest):
        async with state.lock:
            state.received += 1
            state.chat_requests += 1
            chaos = state.chaos.model_copy()
            nth = state.chat_requests

        # fail_every_n vale em qualquer modo
        if chaos.fail_every_n and nth % chaos.fail_every_n == 0:
            resp = _failure(chaos.fail_status, chaos)
        elif chaos.mode == "rate_limit":
            resp = _failure(429, chaos)
        elif chaos.mode == "server_error":
            resp = _failure(chaos.error_status, chaos)
        else:
            if chaos.mode == "slow":
                await sleep(chaos.latency_ms / 1000)  # fora do lock
            last_user = next((m for m in reversed(req.messages) if m.role == "user"), None)
            prompt = _text_of((last_user or req.messages[-1]).content)
            reply = f"[mock] {prompt[:200]}"
            prompt_tokens = sum(len(_text_of(m.content).split()) for m in req.messages)
            completion_tokens = len(reply.split())
            resp = JSONResponse(
                {
                    "id": f"chatcmpl-mock-{uuid.uuid4().hex[:12]}",
                    "object": "chat.completion",
                    "created": int(time.time()),
                    "model": req.model,
                    "choices": [
                        {
                            "index": 0,
                            "message": {"role": "assistant", "content": reply},
                            "finish_reason": "stop",
                        }
                    ],
                    "usage": {
                        "prompt_tokens": prompt_tokens,
                        "completion_tokens": completion_tokens,
                        "total_tokens": prompt_tokens + completion_tokens,
                    },
                }
            )

        await record(resp.status_code)
        return resp

    return app


def load_port() -> int:
    """Lê mock_upstream.port do config.yaml; usa 9001 se ausente."""
    try:
        cfg = yaml.safe_load(CONFIG_PATH.read_text(encoding="utf-8")) or {}
        return int(cfg.get("mock_upstream", {}).get("port", DEFAULT_PORT))
    except (OSError, ValueError, yaml.YAMLError):
        return DEFAULT_PORT


if __name__ == "__main__":
    uvicorn.run(create_app(), host=HOST, port=load_port())
