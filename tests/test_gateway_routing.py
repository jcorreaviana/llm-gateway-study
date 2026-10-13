import httpx
import pytest

from gateway.config import AppConfig
from gateway.main import create_app
from mock_upstream.server import create_app as create_mock_app

MOCK_URL = "http://localhost:9001"
OLLAMA_URL = "http://localhost:11434"
BODY = {"model": "gpt-qualquer", "messages": [{"role": "user", "content": "ola mundo"}]}


def provider(name, type_, url, model, ctx=4096, **kw):
    return {
        "name": name, "type": type_, "base_url": url, "model": model,
        "timeout_s": 5, "context_window": ctx, **kw,
    }


def make_config(force=False, **overrides) -> AppConfig:
    data = {
        "gateway": {"force_provider_enabled": force},
        "chain": ["mock_market", "ollama_qwen", "ollama_llama"],
        "providers": [
            provider("mock_market", "mock", MOCK_URL, "mock-market-1"),
            provider("ollama_qwen", "ollama", OLLAMA_URL, "qwen2.5:3b"),
            provider("ollama_llama", "ollama", OLLAMA_URL, "llama3.2:3b"),
        ],
        **overrides,
    }
    return AppConfig.model_validate(data)


class Ollama:
    """Ollama simulado: responde conforme o model pedido e guarda o que recebeu."""

    def __init__(self, fail=None):
        self.fail = fail or {}  # model -> callable(request) -> Response | raises
        self.seen: list[dict] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        import json

        payload = json.loads(request.content)
        self.seen.append(payload)
        model = payload["model"]
        if model in self.fail:
            return self.fail[model](request)
        return httpx.Response(
            200,
            json={
                "id": "x", "object": "chat.completion", "model": model,
                "choices": [{"index": 0, "message": {"role": "assistant", "content": f"[{model}] ok"},
                             "finish_reason": "stop"}],
            },
        )


def raiser(exc):
    def handler(request):
        raise exc

    return handler


class Env:
    def __init__(self, client, mock_app_client, ollama):
        self.client = client
        self.mock = mock_app_client  # fala direto com o mock upstream (chaos/stats)
        self.ollama = ollama

    async def chat(self, body=BODY, **headers):
        return await self.client.post("/v1/chat/completions", json=body, headers=headers)


@pytest.fixture
def make_env():
    """FÃ¡brica: make_env(config, ollama=..., mock_handler=...) -> Env (async context)."""
    import contextlib

    @contextlib.asynccontextmanager
    async def factory(config=None, ollama=None, mock_handler=None):
        config = config or make_config()
        ollama = ollama or Ollama()
        mock_app = create_mock_app()
        mock_transport = httpx.MockTransport(mock_handler) if mock_handler else httpx.ASGITransport(app=mock_app)
        upstream = httpx.AsyncClient(
            mounts={MOCK_URL: mock_transport, OLLAMA_URL: httpx.MockTransport(ollama)}
        )
        gw = create_app(config, client=upstream)
        async with (
            httpx.AsyncClient(transport=httpx.ASGITransport(app=gw), base_url="http://gw") as c,
            httpx.AsyncClient(transport=httpx.ASGITransport(app=mock_app), base_url="http://mock") as m,
        ):
            yield Env(c, m, ollama)
        await upstream.aclose()

    return factory


async def test_success_on_first_provider_has_no_fallback_header(make_env):
    async with make_env() as env:
        r = await env.chat()
    assert r.status_code == 200
    assert r.headers["x-gateway-provider"] == "mock_market"
    assert "x-gateway-fallback-reason" not in r.headers
    assert r.json()["model"] == "mock-market-1"  # model do cliente substituÃ­do
    assert env.ollama.seen == []


@pytest.mark.parametrize(
    "chaos, reason",
    [
        ({"mode": "rate_limit", "retry_after_seconds": 3}, "rate_limited"),
        ({"mode": "server_error", "error_status": 503}, "server_error"),
        ({"mode": "server_error", "error_status": 500}, "server_error"),
    ],
)
async def test_mock_failure_falls_back_to_qwen(make_env, chaos, reason):
    async with make_env() as env:
        await env.mock.post("/chaos", json=chaos)
        r = await env.chat()
        stats = (await env.mock.get("/stats")).json()
    assert r.status_code == 200
    assert r.headers["x-gateway-provider"] == "ollama_qwen"
    assert r.headers["x-gateway-fallback-reason"] == f"mock_market:{reason}"
    assert r.json()["model"] == "qwen2.5:3b"
    assert env.ollama.seen[0]["model"] == "qwen2.5:3b"  # model do cliente nÃ£o vaza adiante
    assert stats["received"] == 1


@pytest.mark.parametrize(
    "exc, reason",
    [
        (httpx.ReadTimeout("t"), "timeout"),
        (httpx.ConnectTimeout("t"), "timeout"),
        (httpx.ConnectError("c"), "connection_error"),
    ],
)
async def test_network_failures_fall_back(make_env, exc, reason):
    async with make_env(mock_handler=raiser(exc)) as env:
        r = await env.chat()
    assert r.status_code == 200
    assert r.headers["x-gateway-provider"] == "ollama_qwen"
    assert r.headers["x-gateway-fallback-reason"] == f"mock_market:{reason}"


async def test_chain_continues_through_several_failures(make_env):
    ollama = Ollama(fail={"qwen2.5:3b": raiser(httpx.ConnectError("down"))})
    async with make_env(ollama=ollama) as env:
        await env.mock.post("/chaos", json={"mode": "rate_limit"})
        r = await env.chat()
    assert r.headers["x-gateway-provider"] == "ollama_llama"
    assert r.headers["x-gateway-fallback-reason"] == "mock_market:rate_limited,ollama_qwen:connection_error"


async def test_auth_error_falls_back(make_env):
    async with make_env(mock_handler=lambda req: httpx.Response(401, json={"error": {}})) as env:
        r = await env.chat()
    assert r.headers["x-gateway-provider"] == "ollama_qwen"
    assert r.headers["x-gateway-fallback-reason"] == "mock_market:auth_error"


async def test_client_error_is_returned_without_fallback(make_env):
    body = {"error": {"message": "bad", "type": "invalid_request_error", "code": 422}}
    async with make_env(mock_handler=lambda req: httpx.Response(422, json=body)) as env:
        r = await env.chat()
    assert r.status_code == 422
    assert r.json() == body
    assert r.headers["x-gateway-provider"] == "mock_market"
    assert env.ollama.seen == []


async def test_context_too_large_skips_provider(make_env):
    config = make_config()
    config.provider("mock_market").context_window = 5  # ~"ola mundo" estimado em 3 tokens; forÃ§a estouro abaixo
    big = {"messages": [{"role": "user", "content": "x" * 100}]}
    async with make_env(config) as env:
        r = await env.chat(big)
        stats = (await env.mock.get("/stats")).json()
    assert r.status_code == 200
    assert r.headers["x-gateway-provider"] == "ollama_qwen"
    assert r.headers["x-gateway-fallback-reason"] == "mock_market:context_too_large"
    assert stats["received"] == 0  # nem chegou a ser chamado


async def test_disabled_provider_is_skipped_and_reported(make_env):
    config = make_config()
    config.provider("mock_market").enabled = False
    async with make_env(config) as env:
        r = await env.chat()
    assert r.headers["x-gateway-provider"] == "ollama_qwen"
    assert r.headers["x-gateway-fallback-reason"] == "mock_market:disabled"


async def test_all_failing_returns_503_with_reasons(make_env):
    ollama = Ollama(
        fail={
            "qwen2.5:3b": raiser(httpx.ReadTimeout("t")),
            "llama3.2:3b": lambda req: httpx.Response(500),
        }
    )
    async with make_env(ollama=ollama) as env:
        await env.mock.post("/chaos", json={"mode": "rate_limit"})
        r = await env.chat()
    assert r.status_code == 503
    expected = {"mock_market": "rate_limited", "ollama_qwen": "timeout", "ollama_llama": "server_error"}
    assert r.json()["error"]["providers"] == expected
    assert r.headers["x-gateway-fallback-reason"] == (
        "mock_market:rate_limited,ollama_qwen:timeout,ollama_llama:server_error"
    )
    assert "x-gateway-provider" not in r.headers


async def test_stream_true_returns_400(make_env):
    async with make_env() as env:
        r = await env.chat({**BODY, "stream": True})
        stats = (await env.mock.get("/stats")).json()
    assert r.status_code == 400
    assert "stream" in r.json()["error"]["message"]
    assert stats["received"] == 0


async def test_invalid_body_is_rejected(make_env):
    async with make_env() as env:
        r = await env.chat({"messages": []})
    assert r.status_code == 422


async def test_health(make_env):
    async with make_env() as env:
        r = await env.client.get("/health")
    assert r.json() == {"status": "ok"}


async def test_force_header_ignored_when_disabled(make_env):
    async with make_env(make_config(force=False)) as env:
        r = await env.chat(**{"x-gateway-force-provider": "ollama_llama"})
    assert r.headers["x-gateway-provider"] == "mock_market"


async def test_force_header_works_when_enabled_without_fallback(make_env):
    async with make_env(make_config(force=True)) as env:
        r = await env.chat(**{"x-gateway-force-provider": "ollama_llama"})
        assert r.headers["x-gateway-provider"] == "ollama_llama"
        assert "x-gateway-fallback-reason" not in r.headers

    ollama = Ollama(fail={"llama3.2:3b": lambda req: httpx.Response(500)})
    async with make_env(make_config(force=True), ollama=ollama) as env:
        r = await env.chat(**{"x-gateway-force-provider": "ollama_llama"})
    assert r.status_code == 503  # sem fallback para outro provedor
    assert r.json()["error"]["providers"] == {"ollama_llama": "server_error"}


async def test_force_unknown_provider_returns_400(make_env):
    async with make_env(make_config(force=True)) as env:
        r = await env.chat(**{"x-gateway-force-provider": "ghost"})
    assert r.status_code == 400


async def test_anthropic_refuses_even_with_real_api_enabled_and_key(make_env, monkeypatch):
    monkeypatch.setenv("ENABLE_REAL_API", "1")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "fake-" + "key-for-test")
    opened: list[httpx.Request] = []

    def tripwire(request):
        opened.append(request)
        return httpx.Response(200, json={})

    config = make_config(
        chain=["anthropic", "mock_market"],
        providers=[
            provider("anthropic", "anthropic", "https://api.anthropic.com", "claude-haiku-5-5", enabled=True),
            provider("mock_market", "mock", MOCK_URL, "mock-market-1"),
        ],
    )
    upstream = httpx.AsyncClient(
        mounts={"all://": httpx.MockTransport(tripwire), MOCK_URL: httpx.ASGITransport(app=create_mock_app())}
    )
    gw = create_app(config, client=upstream)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=gw), base_url="http://gw") as c:
        r = await c.post("/v1/chat/completions", json=BODY)
    await upstream.aclose()
    assert r.status_code == 200
    assert r.headers["x-gateway-provider"] == "mock_market"
    assert r.headers["x-gateway-fallback-reason"] == "anthropic:real_api_blocked"
    assert opened == []  # nenhuma conexÃ£o aberta


async def test_logs_do_not_contain_prompt(make_env, caplog):
    secret_prompt = "frase-secreta-do-usuario"
    caplog.set_level("DEBUG")
    async with make_env() as env:
        await env.mock.post("/chaos", json={"mode": "rate_limit"})
        await env.chat({"messages": [{"role": "user", "content": secret_prompt}]})
    assert secret_prompt not in caplog.text
    assert "request_id=" in caplog.text
