"""Montagem de um gateway com mocks para os testes do Marco 5 (nenhuma rede externa)."""

import contextlib
import json

import httpx

from gateway.budget import Budget
from gateway.config import AppConfig
from gateway.main import create_app

MOCK_URL = "http://localhost:9001"
ANTHROPIC_HOST = "https://api.anthropic.com"
BODY = {"messages": [{"role": "user", "content": "ola mundo"}]}


def fake_key() -> str:
    """Chave falsa montada em runtime (o código-fonte não contém nada com cara de chave)."""
    return "sk-" + "ant-" + "falsa1234567890abcdef"


def openai_ok(request: httpx.Request) -> httpx.Response:
    return httpx.Response(200, json={
        "model": "mock-market-1",
        "choices": [{"index": 0, "message": {"role": "assistant", "content": "ola do mock"}}],
        "usage": {"prompt_tokens": 3, "completion_tokens": 2, "total_tokens": 5},
    })


def anthropic_ok_json() -> dict:
    return {
        "id": "msg_1", "type": "message", "role": "assistant", "model": "claude-haiku-5-5",
        "content": [{"type": "text", "text": "resposta real simulada"}],
        "stop_reason": "end_turn",
        "usage": {"input_tokens": 5, "output_tokens": 2},
    }


def provider_cfg(name, type_, model, url=None, **kw):
    cfg = {"name": name, "type": type_, "model": model, "timeout_s": 5, "context_window": 8192, **kw}
    if url:
        cfg["base_url"] = url
    return cfg


def make_config(*, anthropic_enabled=True, force=False, anthropic_kw=None, mock_kw=None,
                chain=None, **extra) -> AppConfig:
    return AppConfig.model_validate({
        "gateway": {"force_provider_enabled": force},
        "chain": chain or ["anthropic", "mock_market"],
        "providers": [
            provider_cfg("anthropic", "anthropic", "claude-haiku-5-5", enabled=anthropic_enabled,
                         context_window=200000, **(anthropic_kw or {})),
            provider_cfg("mock_market", "mock", "mock-market-1", MOCK_URL, **(mock_kw or {})),
        ],
        **extra,
    })


class Gw:
    def __init__(self, client, app, anthropic_calls, mock_calls, budget):
        self.client, self.app, self.budget = client, app, budget
        self.anthropic_calls, self.mock_calls = anthropic_calls, mock_calls

    async def chat(self, body=BODY, **headers):
        return await self.client.post("/v1/chat/completions", json=body, headers=headers)

    async def metrics_text(self) -> str:
        return (await self.client.get("/metrics")).text

    async def status(self) -> dict:
        return (await self.client.get("/status")).json()


@contextlib.asynccontextmanager
async def gateway(tmp_path, config=None, mock_handler=openai_ok, anthropic_handler=None,
                  budget_limit=30, now=None, **app_kw):
    anthropic_calls: list[httpx.Request] = []
    mock_calls: list[httpx.Request] = []

    def anthropic(request):
        anthropic_calls.append(request)
        if anthropic_handler:
            return anthropic_handler(request)
        return httpx.Response(200, json=anthropic_ok_json())

    def mock(request):
        mock_calls.append(request)
        return mock_handler(request)

    upstream = httpx.AsyncClient(mounts={
        ANTHROPIC_HOST: httpx.MockTransport(anthropic),
        MOCK_URL: httpx.MockTransport(mock),
    })
    budget = app_kw.pop("budget", None) or Budget(tmp_path / "data" / "budget.json", budget_limit)
    kw = dict(app_kw)
    if now:
        kw["now"] = now
    app = create_app(config or make_config(), client=upstream, budget=budget, **kw)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://gw") as c:
        yield Gw(c, app, anthropic_calls, mock_calls, budget)
    await upstream.aclose()


def read_jsonl(path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]
