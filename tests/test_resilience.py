"""Resiliência no gateway: token bucket, breaker e cooldown. Sem rede externa e sem sleep:
o tempo vem de um relógio falso injetado em `create_app(now=...)`."""

import asyncio
import contextlib
import json
import logging

import httpx
import pytest
from pydantic import ValidationError

from gateway.config import AppConfig
from gateway.main import create_app, warmup_providers
from gateway.providers import FailureReason, build_providers
from gateway.router import Router
from mock_upstream.server import create_app as create_mock_app

MOCK_URL = "http://localhost:9001"
OLLAMA_URL = "http://localhost:11434"
BODY = {"messages": [{"role": "user", "content": "ola mundo"}]}


class Clock:
    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t

    def advance(self, s: float) -> None:
        self.t += s


def prov(name, type_, url, model, **kw):
    return {"name": name, "type": type_, "base_url": url, "model": model,
            "timeout_s": 5, "context_window": 4096, **kw}


def make_config(mock=None, qwen=None, llama=None, force=False, chain=None) -> AppConfig:
    return AppConfig.model_validate({
        "gateway": {"force_provider_enabled": force},
        "chain": chain or ["mock_market", "ollama_qwen", "ollama_llama"],
        "providers": [
            prov("mock_market", "mock", MOCK_URL, "mock-market-1", **(mock or {})),
            prov("ollama_qwen", "ollama", OLLAMA_URL, "qwen2.5:3b", **(qwen or {})),
            prov("ollama_llama", "ollama", OLLAMA_URL, "llama3.2:3b", **(llama or {})),
        ],
    })


class Ollama:
    def __init__(self, fail_models=()):
        self.fail_models = set(fail_models)
        self.seen: list[dict] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        self.seen.append(payload)
        if payload["model"] in self.fail_models:
            return httpx.Response(500, json={"error": "boom"})
        return httpx.Response(200, json={
            "model": payload["model"],
            "choices": [{"index": 0, "message": {"role": "assistant", "content": "ok"}}],
        })


class Env:
    def __init__(self, gw, mock, ollama, clock, app):
        self.gw, self.mock, self.ollama, self.clock, self.app = gw, mock, ollama, clock, app

    async def chat(self, body=BODY, **headers):
        return await self.gw.post("/v1/chat/completions", json=body, headers=headers)

    async def chaos(self, **cfg):
        assert (await self.mock.post("/chaos", json=cfg)).status_code == 200

    async def mock_received(self) -> int:
        return (await self.mock.get("/stats")).json()["received"]

    async def provider_status(self, name):
        return (await self.gw.get("/status")).json()["providers"][name]


@pytest.fixture
def make_env():
    @contextlib.asynccontextmanager
    async def factory(config=None, ollama=None, mock_handler=None):
        config = config or make_config()
        ollama, clock = ollama or Ollama(), Clock()
        mock_app = create_mock_app()
        mock_transport = httpx.MockTransport(mock_handler) if mock_handler else httpx.ASGITransport(app=mock_app)
        upstream = httpx.AsyncClient(mounts={MOCK_URL: mock_transport, OLLAMA_URL: httpx.MockTransport(ollama)})
        gw = create_app(config, client=upstream, now=clock)
        async with (
            httpx.AsyncClient(transport=httpx.ASGITransport(app=gw), base_url="http://gw") as g,
            httpx.AsyncClient(transport=httpx.ASGITransport(app=mock_app), base_url="http://mock") as m,
        ):
            yield Env(g, m, ollama, clock, gw)
        await upstream.aclose()

    return factory


def reasons(r) -> str:
    return r.headers.get("x-gateway-fallback-reason", "")


# --------------------------------------------------------------------------- config
@pytest.mark.parametrize("override", [
    {"rate_limit": {"rpm": 0}}, {"rate_limit": {"rpm": -1}},
    {"rate_limit": {"tpm": 0}}, {"rate_limit": {"tpm": -10}},
    {"breaker": {"failure_threshold": 0}}, {"breaker": {"failure_threshold": -1}},
    {"breaker": {"cooldown_s": 0}}, {"breaker": {"cooldown_s": -5}},
    {"breaker": {"half_open_max_calls": 0}}, {"breaker": {"half_open_max_calls": -1}},
    {"default_retry_after_s": 0}, {"default_retry_after_s": -1},
    {"breaker": {"campo_inventado": 1}},
    {"warmup": True},  # só vale para Ollama; aqui o provedor é mock
])
def test_invalid_resilience_config_fails_at_startup(override):
    with pytest.raises(ValidationError):
        make_config(mock=override)


def test_resilience_config_defaults():
    cfg = make_config().provider("mock_market")
    assert cfg.rate_limit is None and cfg.warmup is False
    assert (cfg.breaker.failure_threshold, cfg.breaker.cooldown_s, cfg.breaker.half_open_max_calls) == (3, 30, 1)
    assert cfg.default_retry_after_s > 0
    ok = make_config(mock={"rate_limit": {"rpm": 5, "tpm": None}}).provider("mock_market")
    assert ok.rate_limit.rpm == 5 and ok.rate_limit.tpm is None


def test_repo_config_yaml_has_no_artificial_demo_limits():
    from gateway.config import load_config

    cfg = load_config()
    assert cfg.provider("mock_market").rate_limit is None
    assert cfg.provider("ollama_qwen").rate_limit is None
    assert all(not p.warmup for p in cfg.providers)


# --------------------------------------------------------------------------- breaker
async def test_breaker_opens_after_consecutive_failures_and_stops_calling_provider(make_env):
    async with make_env() as env:
        await env.chaos(mode="server_error")
        for _ in range(3):
            r = await env.chat()
            assert r.status_code == 200 and reasons(r) == "mock_market:server_error"
        assert (await env.provider_status("mock_market"))["breaker"]["state"] == "open"

        r = await env.chat()
        assert r.headers["x-gateway-provider"] == "ollama_qwen"
        assert reasons(r) == "mock_market:breaker_open"
        assert await env.mock_received() == 3  # o 4º não chegou ao mock


async def test_success_resets_consecutive_failures(make_env):
    async with make_env() as env:
        await env.chaos(mode="server_error")
        await env.chat(), await env.chat()
        await env.chaos(mode="normal")
        assert (await env.chat()).headers["x-gateway-provider"] == "mock_market"
        await env.chaos(mode="server_error")
        await env.chat(), await env.chat()
        st = await env.provider_status("mock_market")
        assert st["breaker"] == {"state": "closed", "consecutive_failures": 2, "open_remaining_s": None}


async def _open_breaker(env):
    await env.chaos(mode="server_error")
    for _ in range(3):
        await env.chat()


async def test_half_open_probe_success_closes(make_env):
    async with make_env() as env:
        await _open_breaker(env)
        env.clock.advance(29)
        assert "breaker_open" in reasons(await env.chat())
        env.clock.advance(1)
        assert (await env.provider_status("mock_market"))["breaker"]["state"] == "half_open"

        await env.chaos(mode="normal")
        r = await env.chat()  # a sonda
        assert r.headers["x-gateway-provider"] == "mock_market" and reasons(r) == ""
        assert (await env.provider_status("mock_market"))["breaker"]["state"] == "closed"


async def test_half_open_probe_failure_reopens(make_env):
    async with make_env() as env:
        await _open_breaker(env)
        env.clock.advance(30)
        before = await env.mock_received()
        r = await env.chat()  # a sonda falha
        assert reasons(r) == "mock_market:server_error"
        assert await env.mock_received() == before + 1
        br = (await env.provider_status("mock_market"))["breaker"]
        assert br["state"] == "open" and br["open_remaining_s"] == 30
        assert "breaker_open" in reasons(await env.chat())


async def test_half_open_allows_only_limited_concurrent_probes(make_env):
    gate, started = asyncio.Event(), asyncio.Event()

    async def slow_mock(request):
        started.set()
        await gate.wait()
        return httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}]})

    cfg = make_config(mock={"breaker": {"failure_threshold": 1, "half_open_max_calls": 1}})
    async with make_env(cfg, mock_handler=slow_mock) as env:
        br = env.app.state.router.guards["mock_market"].breaker
        await br.record_failure(await br.acquire())  # abre (limiar 1)
        env.clock.advance(30)

        probe = asyncio.create_task(env.chat())
        await started.wait()
        second = await env.chat()  # a única sonda está em andamento: o mock é pulado
        assert second.headers["x-gateway-provider"] == "ollama_qwen"
        assert reasons(second) == "mock_market:breaker_open"
        gate.set()
        assert (await probe).headers["x-gateway-provider"] == "mock_market"
        assert (await env.provider_status("mock_market"))["breaker"]["state"] == "closed"


# --------------------------------------------------------------------------- 429 / cooldown
async def test_429_triggers_cooldown_without_counting_for_breaker(make_env):
    async with make_env() as env:
        await env.chaos(mode="rate_limit", retry_after_seconds=5)
        r = await env.chat()
        assert reasons(r) == "mock_market:rate_limited"
        st = await env.provider_status("mock_market")
        assert st["breaker"]["consecutive_failures"] == 0 and st["breaker"]["state"] == "closed"
        assert st["cooldown_remaining_s"] == 5

        for _ in range(4):  # durante o cooldown, o mock não recebe nada
            env.clock.advance(1)
            r = await env.chat()
            assert reasons(r) == "mock_market:cooling_down"
        assert await env.mock_received() == 1

        env.clock.advance(1)  # 5s completos
        await env.chaos(mode="normal")
        r = await env.chat()
        assert r.headers["x-gateway-provider"] == "mock_market" and reasons(r) == ""
        assert await env.mock_received() == 2  # voltou a ser tentado


async def test_many_429s_never_open_the_breaker(make_env):
    async with make_env() as env:
        await env.chaos(mode="rate_limit", retry_after_seconds=1)
        for _ in range(6):
            await env.chat()
            env.clock.advance(1)
        assert (await env.provider_status("mock_market"))["breaker"]["state"] == "closed"


async def test_429_without_retry_after_uses_default(make_env):
    def no_header(request):
        return httpx.Response(429, json={"error": {"message": "slow down"}})

    cfg = make_config(mock={"default_retry_after_s": 7})
    async with make_env(cfg, mock_handler=no_header) as env:
        r = await env.chat()
        assert reasons(r) == "mock_market:rate_limited"
        assert (await env.provider_status("mock_market"))["cooldown_remaining_s"] == 7
        env.clock.advance(7)
        assert (await env.provider_status("mock_market"))["cooldown_remaining_s"] == 0


# --------------------------------------------------------------------------- token bucket local
async def test_local_rate_limit_skips_provider_then_recovers(make_env):
    cfg = make_config(mock={"rate_limit": {"rpm": 2}})
    async with make_env(cfg) as env:
        assert (await env.chat()).headers["x-gateway-provider"] == "mock_market"
        assert (await env.chat()).headers["x-gateway-provider"] == "mock_market"
        r = await env.chat()
        assert r.headers["x-gateway-provider"] == "ollama_qwen"
        assert reasons(r) == "mock_market:rate_limit_local"
        assert await env.mock_received() == 2

        env.clock.advance(30)  # 2 rpm = 1 a cada 30s
        assert (await env.chat()).headers["x-gateway-provider"] == "mock_market"


async def test_local_tpm_uses_real_usage(make_env):
    cfg = make_config(mock={"rate_limit": {"tpm": 60}})
    async with make_env(cfg) as env:
        await env.chat()
        level = (await env.provider_status("mock_market"))["buckets"]["tpm"]["level"]
    # estimativa 3; o mock reporta total_tokens=5 (prompt + resposta): ajusta o consumo para 5
    assert level == 55


# --------------------------------------------------------------------------- cadeia completa
@pytest.mark.parametrize("reason", ["rate_limit_local", "breaker_open", "cooling_down"])
async def test_full_chain_fallback_for_each_new_reason(make_env, reason):
    mock_cfg = {
        "rate_limit_local": {"rate_limit": {"rpm": 1}},
        "breaker_open": {"breaker": {"failure_threshold": 1}},
        "cooling_down": {},
    }[reason]
    ollama = Ollama(fail_models={"qwen2.5:3b"})  # qwen também falha -> cai até o llama
    async with make_env(make_config(mock=mock_cfg), ollama=ollama) as env:
        if reason == "rate_limit_local":
            await env.chat()  # gasta o único token
        elif reason == "breaker_open":
            await env.chaos(mode="server_error")
            await env.chat()
        else:
            await env.chaos(mode="rate_limit", retry_after_seconds=10)
            await env.chat()
        env.ollama.seen.clear()

        r = await env.chat()
        assert r.status_code == 200
        assert r.headers["x-gateway-provider"] == "ollama_llama"
        assert reasons(r) == f"mock_market:{reason},ollama_qwen:server_error"
        assert [p["model"] for p in env.ollama.seen] == ["qwen2.5:3b", "llama3.2:3b"]


async def test_all_unavailable_returns_503_with_reasons_and_retry_after(make_env):
    cfg = make_config(
        qwen={"rate_limit": {"rpm": 1}},
        llama={"breaker": {"failure_threshold": 1, "cooldown_s": 20}},
    )
    ollama = Ollama(fail_models={"llama3.2:3b"})
    async with make_env(cfg, ollama=ollama) as env:
        await env.chaos(mode="rate_limit", retry_after_seconds=10)
        assert (await env.chat()).headers["x-gateway-provider"] == "ollama_qwen"  # gasta o balde

        r = await env.chat()  # mock em cooldown, qwen sem balde, llama falha e abre
        assert r.status_code == 503
        assert r.json()["error"]["providers"] == {
            "mock_market": "cooling_down", "ollama_qwen": "rate_limit_local", "ollama_llama": "server_error",
        }
        assert r.headers["Retry-After"] == "10"  # menor espera: o cooldown do mock (qwen: 60s)

        env.clock.advance(3)
        r = await env.chat()
        assert r.json()["error"]["providers"] == {
            "mock_market": "cooling_down", "ollama_qwen": "rate_limit_local", "ollama_llama": "breaker_open",
        }
        assert r.headers["Retry-After"] == "7"
        assert reasons(r) == "mock_market:cooling_down,ollama_qwen:rate_limit_local,ollama_llama:breaker_open"


async def test_503_without_waiting_info_has_no_retry_after(make_env):
    cfg = make_config(mock={"enabled": False}, qwen={"enabled": False}, llama={"enabled": False})
    async with make_env(cfg) as env:
        r = await env.chat()
    assert r.status_code == 503 and "Retry-After" not in r.headers


async def test_force_provider_respects_cooldown_bucket_and_breaker(make_env):
    cfg = make_config(force=True, qwen={"rate_limit": {"rpm": 1}}, llama={"breaker": {"failure_threshold": 1}})
    ollama = Ollama(fail_models={"llama3.2:3b"})
    async with make_env(cfg, ollama=ollama) as env:
        await env.chaos(mode="rate_limit", retry_after_seconds=10)
        await env.chat()  # mock em cooldown; o qwen responde e esvazia o balde dele

        r = await env.chat(**{"x-gateway-force-provider": "mock_market"})
        assert r.status_code == 503 and r.json()["error"]["providers"] == {"mock_market": "cooling_down"}
        assert r.headers["Retry-After"] == "10"

        r = await env.chat(**{"x-gateway-force-provider": "ollama_qwen"})
        assert r.status_code == 503 and r.json()["error"]["providers"] == {"ollama_qwen": "rate_limit_local"}

        r = await env.chat(**{"x-gateway-force-provider": "ollama_llama"})
        assert r.json()["error"]["providers"] == {"ollama_llama": "server_error"}  # abre o breaker
        r = await env.chat(**{"x-gateway-force-provider": "ollama_llama"})
        assert r.json()["error"]["providers"] == {"ollama_llama": "breaker_open"}
        assert [p["model"] for p in env.ollama.seen].count("llama3.2:3b") == 1


# --------------------------------------------------------------------------- /status
async def test_status_reflects_state_and_exposes_no_secrets(make_env, monkeypatch):
    secret = "sk-" + "ant-" + "SEGREDO123456789"
    monkeypatch.setenv("ANTHROPIC_API_KEY", secret)
    cfg = make_config(mock={"rate_limit": {"rpm": 5, "tpm": 1000}})
    async with make_env(cfg) as env:
        await env.chaos(mode="server_error")
        for _ in range(3):
            await env.chat()
        r = await env.gw.get("/status")
    body = r.json()["providers"]
    assert set(body) == {"mock_market", "ollama_qwen", "ollama_llama"}
    assert body["mock_market"]["breaker"]["state"] == "open"
    assert body["mock_market"]["buckets"]["rpm"]["capacity"] == 5
    assert body["mock_market"]["buckets"]["rpm"]["level"] == 2
    assert body["mock_market"]["buckets"]["tpm"]["capacity"] == 1000
    assert body["ollama_qwen"]["buckets"] == {"rpm": None, "tpm": None}
    assert body["ollama_qwen"]["breaker"]["state"] == "closed"
    assert body["ollama_qwen"]["cooldown_remaining_s"] == 0
    text = r.text
    assert secret not in text and "localhost" not in text and "base_url" not in text


async def test_status_is_read_only(make_env):
    async with make_env() as env:
        assert (await env.gw.post("/status")).status_code == 405


# --------------------------------------------------------------------------- timeout / cancelamento
async def test_timeout_cancels_request_for_real_and_counts_for_breaker():
    cancelled = asyncio.Event()

    async def hang(request: httpx.Request) -> httpx.Response:
        try:
            await asyncio.Event().wait()  # nunca termina sozinho
        except asyncio.CancelledError:
            cancelled.set()
            raise

    cfg = make_config(
        mock={"timeout_s": 0.02, "breaker": {"failure_threshold": 1}}, chain=["mock_market"]
    )
    clock = Clock()
    http = httpx.AsyncClient(mounts={MOCK_URL: httpx.MockTransport(hang)})
    router = Router(cfg, build_providers(cfg.providers, http), clock)
    me = asyncio.current_task()

    from gateway.router import AllProvidersFailed

    with pytest.raises(AllProvidersFailed) as exc:
        await router.route(BODY, "rid")
    assert exc.value.attempts == [("mock_market", FailureReason.TIMEOUT)]
    assert cancelled.is_set()  # a requisição pendente foi cancelada de verdade
    assert [t for t in asyncio.all_tasks() if t is not me and not t.done()] == []

    with pytest.raises(AllProvidersFailed) as exc:  # timeout contou: breaker abriu (limiar 1)
        await router.route(BODY, "rid2")
    assert exc.value.attempts == [("mock_market", FailureReason.BREAKER_OPEN)]
    await http.aclose()


async def test_cancelled_request_releases_probe_slot():
    started = asyncio.Event()

    async def hang(request):
        started.set()
        await asyncio.Event().wait()

    cfg = make_config(mock={"breaker": {"failure_threshold": 1}}, chain=["mock_market"])
    clock = Clock()
    http = httpx.AsyncClient(mounts={MOCK_URL: httpx.MockTransport(hang)})
    router = Router(cfg, build_providers(cfg.providers, http), clock)
    br = router.guards["mock_market"].breaker
    adm = await br.acquire()
    await br.record_failure(adm)
    clock.advance(30)

    task = asyncio.create_task(router.route(BODY, "rid"))
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert (await br.acquire()).probe  # a vaga da sonda foi devolvida
    await http.aclose()


# --------------------------------------------------------------------------- warmup
def _router_with_warmup(handler, caplog=None):
    cfg = make_config(qwen={"warmup": True}, chain=["ollama_qwen", "ollama_llama"])
    http = httpx.AsyncClient(mounts={OLLAMA_URL: httpx.MockTransport(handler)})
    return Router(cfg, build_providers(cfg.providers, http), Clock()), http


async def test_warmup_sends_minimal_request_only_to_enabled_warmup_providers():
    ollama = Ollama()
    router, http = _router_with_warmup(ollama)
    await warmup_providers(router)
    assert [(p["model"], p["max_tokens"]) for p in ollama.seen] == [("qwen2.5:3b", 1)]
    # warmup não passa pelo breaker nem gasta balde
    assert router.guards["ollama_qwen"].breaker.snapshot()["consecutive_failures"] == 0
    await http.aclose()


async def test_warmup_failure_only_logs_warning(caplog):
    router, http = _router_with_warmup(Ollama(fail_models={"qwen2.5:3b"}))
    with caplog.at_level(logging.WARNING, logger="gateway"):
        await warmup_providers(router)  # não levanta
    assert any("warmup" in r.message and "server_error" in r.message for r in caplog.records)
    assert "hi" not in caplog.text.split("warmup")[-1]  # sem conteúdo do prompt
    await http.aclose()
