"""AnthropicProvider: só httpx.MockTransport. Nenhuma conexão real é aberta; o handler é a
prova (qualquer requisição que chegasse ao 'servidor' passaria por ele)."""

import json
import logging

import httpx
import pytest

from gateway.budget import Budget
from gateway.config import ProviderConfig
from gateway.providers import (
    ANTHROPIC_URL,
    AnthropicProvider,
    FailureReason,
    ProviderFailure,
    ProviderSuccess,
)
from tests.gw_helpers import BODY, anthropic_ok_json, fake_key, gateway, make_config, read_jsonl


def make_provider(tmp_path, handler=None, limit=30, **cfg_kw):
    seen: list[httpx.Request] = []

    def h(request):
        seen.append(request)
        return handler(request) if handler else httpx.Response(200, json=anthropic_ok_json())

    cfg = ProviderConfig(name="anthropic", type="anthropic", model="claude-haiku-5-5",
                         timeout_s=5, context_window=200000, enabled=True, **cfg_kw)
    budget = Budget(tmp_path / "b.json", limit)
    client = httpx.AsyncClient(transport=httpx.MockTransport(h))
    return AnthropicProvider(cfg, client, budget), budget, seen, client


@pytest.fixture
def real_on(monkeypatch):
    monkeypatch.setenv("ENABLE_REAL_API", "1")
    monkeypatch.setenv("ANTHROPIC_API_KEY", fake_key())


def used(budget) -> int:
    return budget.status().used


# ------------------------------------------------------------------------ bloqueios
async def test_blocked_when_flag_is_off_or_absent(tmp_path, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", fake_key())
    p, budget, seen, _ = make_provider(tmp_path)
    for value in (None, "0", "", "true"):
        if value is None:
            monkeypatch.delenv("ENABLE_REAL_API", raising=False)
        else:
            monkeypatch.setenv("ENABLE_REAL_API", value)
        r = await p.complete(BODY)
        assert isinstance(r, ProviderFailure) and r.reason is FailureReason.REAL_API_BLOCKED
    assert seen == [] and used(budget) == 0


async def test_blocked_without_key(tmp_path, monkeypatch):
    monkeypatch.setenv("ENABLE_REAL_API", "1")
    p, budget, seen, _ = make_provider(tmp_path)
    r = await p.complete(BODY)
    assert r.reason is FailureReason.REAL_API_BLOCKED
    monkeypatch.setenv("ANTHROPIC_API_KEY", "")
    assert (await p.complete(BODY)).reason is FailureReason.REAL_API_BLOCKED
    assert seen == [] and used(budget) == 0


async def test_blocked_when_provider_disabled(tmp_path, real_on):
    p, budget, seen, _ = make_provider(tmp_path)
    p.cfg = p.cfg.model_copy(update={"enabled": False})
    assert (await p.complete(BODY)).reason is FailureReason.REAL_API_BLOCKED
    assert seen == [] and used(budget) == 0


async def test_blocked_when_budget_exhausted(tmp_path, real_on):
    p, budget, seen, _ = make_provider(tmp_path, limit=1)
    assert budget.reserve()
    assert (await p.complete(BODY)).reason is FailureReason.BUDGET_EXHAUSTED
    assert seen == [] and used(budget) == 1


async def test_blocked_when_budget_file_is_corrupt(tmp_path, real_on):
    p, budget, seen, _ = make_provider(tmp_path)
    budget.path.write_text("lixo", encoding="utf-8")
    assert (await p.complete(BODY)).reason is FailureReason.BUDGET_EXHAUSTED
    assert seen == [] and budget.path.read_text(encoding="utf-8") == "lixo"


async def test_blocked_without_budget_object(tmp_path, real_on):
    p, _, seen, _ = make_provider(tmp_path)
    p._budget = None
    assert (await p.complete(BODY)).reason is FailureReason.BUDGET_EXHAUSTED
    assert seen == []


async def test_input_too_large_blocks_without_spending_budget(tmp_path, real_on):
    p, budget, seen, _ = make_provider(tmp_path, real_api_max_input_tokens=100)
    big = {"messages": [{"role": "user", "content": "x" * 1000}]}  # ~250 tokens estimados
    assert (await p.complete(big)).reason is FailureReason.INPUT_TOO_LARGE
    assert seen == [] and used(budget) == 0


async def test_default_input_limit_is_1000_tokens(tmp_path, real_on):
    p, _, seen, _ = make_provider(tmp_path)
    assert p.cfg.real_api_max_input_tokens == 1000
    assert (await p.complete({"messages": [{"role": "user", "content": "x" * 4100}]})).reason \
        is FailureReason.INPUT_TOO_LARGE
    assert isinstance(await p.complete({"messages": [{"role": "user", "content": "x" * 3000}]}), ProviderSuccess)
    assert len(seen) == 1


# ---------------------------------------------------------------- request e resposta
async def test_translates_request_and_response(tmp_path, real_on):
    p, budget, seen, _ = make_provider(tmp_path)
    req = {
        "model": "gpt-qualquer", "temperature": 0.3, "max_tokens": 10,
        "messages": [
            {"role": "system", "content": "seja breve"},
            {"role": "user", "content": "oi"},
            {"role": "assistant", "content": "ola"},
            {"role": "user", "content": [{"type": "text", "text": "tudo bem?"}]},
        ],
    }
    r = await p.complete(req)
    assert isinstance(r, ProviderSuccess)
    sent = seen[0]
    assert str(sent.url) == ANTHROPIC_URL and sent.method == "POST"
    assert sent.headers["x-api-key"] == fake_key()
    assert sent.headers["anthropic-version"] == "2023-06-01"
    body = json.loads(sent.content)
    assert body == {
        "model": "claude-haiku-5-5",  # do config, nunca do request
        "max_tokens": 10, "temperature": 0.3, "system": "seja breve",
        "messages": [
            {"role": "user", "content": "oi"},
            {"role": "assistant", "content": "ola"},
            {"role": "user", "content": "tudo bem?"},
        ],
    }
    data = r.data
    assert data["choices"][0]["message"] == {"role": "assistant", "content": "resposta real simulada"}
    assert data["choices"][0]["finish_reason"] == "stop"
    assert data["usage"] == {"prompt_tokens": 5, "completion_tokens": 2, "total_tokens": 7}
    assert used(budget) == 1


@pytest.mark.parametrize("requested, expected", [
    (1000, 50), (50, 50), (10, 10), (None, 50), (0, 50), (-5, 50), ("muito", 50), (True, 50),
])
async def test_max_tokens_is_capped(tmp_path, real_on, requested, expected):
    p, _, seen, _ = make_provider(tmp_path)
    req = {"messages": BODY["messages"]}
    if requested is not None:
        req["max_tokens"] = requested
    await p.complete(req)
    assert json.loads(seen[0].content)["max_tokens"] == expected


async def test_max_tokens_cap_is_configurable(tmp_path, real_on):
    p, _, seen, _ = make_provider(tmp_path, max_tokens_cap=7)
    await p.complete({**BODY, "max_tokens": 500})
    assert json.loads(seen[0].content)["max_tokens"] == 7


async def test_max_tokens_stop_reason_maps_to_length(tmp_path, real_on):
    data = {**anthropic_ok_json(), "stop_reason": "max_tokens"}
    p, *_ = make_provider(tmp_path, lambda r: httpx.Response(200, json=data))
    r = await p.complete(BODY)
    assert r.data["choices"][0]["finish_reason"] == "length"


# ----------------------------------------------------------------------- classificação
@pytest.mark.parametrize("status, reason", [
    (429, FailureReason.RATE_LIMITED), (500, FailureReason.SERVER_ERROR),
    (502, FailureReason.SERVER_ERROR), (529, FailureReason.SERVER_ERROR),
    (401, FailureReason.AUTH_ERROR), (403, FailureReason.AUTH_ERROR),
    (400, FailureReason.CLIENT_ERROR), (404, FailureReason.CLIENT_ERROR),
    (422, FailureReason.CLIENT_ERROR),
])
async def test_http_failures_are_classified_and_still_count_in_budget(tmp_path, real_on, status, reason):
    p, budget, seen, _ = make_provider(tmp_path, lambda r: httpx.Response(status, json={"error": "x"}))
    r = await p.complete(BODY)
    assert isinstance(r, ProviderFailure) and r.reason is reason and r.status_code == status
    assert used(budget) == 1  # falha também gasta a reserva


async def test_429_parses_retry_after(tmp_path, real_on):
    p, *_ = make_provider(tmp_path, lambda r: httpx.Response(429, headers={"retry-after": "7"}, json={}))
    r = await p.complete(BODY)
    assert r.reason is FailureReason.RATE_LIMITED and r.retry_after_s == 7.0


@pytest.mark.parametrize("exc, reason", [
    (httpx.ReadTimeout("t"), FailureReason.TIMEOUT),
    (httpx.ConnectTimeout("t"), FailureReason.TIMEOUT),
    (httpx.ConnectError("c"), FailureReason.CONNECTION_ERROR),
])
async def test_network_failures_are_classified_and_count(tmp_path, real_on, exc, reason):
    def boom(request):
        raise exc

    p, budget, *_ = make_provider(tmp_path, boom)
    r = await p.complete(BODY)
    assert r.reason is reason and used(budget) == 1


@pytest.mark.parametrize("response", [
    httpx.Response(200, content=b"nao e json"),
    httpx.Response(200, json={"content": "nao e lista"}),
    httpx.Response(200, json=[1, 2]),
])
async def test_invalid_2xx_is_invalid_response(tmp_path, real_on, response):
    p, *_ = make_provider(tmp_path, lambda r: response)
    assert (await p.complete(BODY)).reason is FailureReason.INVALID_RESPONSE


async def test_error_loop_cannot_exceed_budget(tmp_path, real_on):
    p, budget, seen, _ = make_provider(tmp_path, lambda r: httpx.Response(500, json={}), limit=3)
    reasons = [(await p.complete(BODY)).reason for _ in range(6)]
    assert len(seen) == 3  # só 3 chegaram ao "servidor"
    assert reasons[3:] == [FailureReason.BUDGET_EXHAUSTED] * 3
    assert used(budget) == 3


async def test_base_url_cannot_be_redirected_by_request(tmp_path, real_on):
    p, _, seen, _ = make_provider(tmp_path)
    await p.complete({**BODY, "base_url": "http://evil.example", "url": "http://evil.example"})
    assert str(seen[0].url) == ANTHROPIC_URL


# ---------------------------------------------------------------------- chave nunca vaza
async def test_key_never_appears_in_logs_failures_or_client_error_body(tmp_path, real_on, caplog):
    key = fake_key()
    caplog.set_level(logging.DEBUG)
    # O upstream "ecoa" a chave no corpo do 400 e no do 401: ela deve ser mascarada.
    for status in (400, 401, 500):
        p, *_ = make_provider(tmp_path, lambda r, s=status: httpx.Response(s, text=f"erro {key} fim"))
        r = await p.complete(BODY)
        assert key not in repr(r) and key not in (r.body or b"").decode()
    assert key not in caplog.text


async def test_key_is_read_at_use_time(tmp_path, real_on, monkeypatch):
    p, _, seen, _ = make_provider(tmp_path)
    other = "sk-" + "ant-" + "outra0987654321zyx"
    monkeypatch.setenv("ANTHROPIC_API_KEY", other)
    await p.complete(BODY)
    assert seen[0].headers["x-api-key"] == other


# --------------------------------------------------- pelo gateway: fallback sem breaker
async def test_gateway_budget_exhausted_falls_back_without_touching_breaker(tmp_path, real_on):
    async with gateway(tmp_path, budget_limit=1) as gw:
        assert gw.budget.reserve()  # esgota
        for _ in range(6):  # bem acima do failure_threshold padrão (3)
            r = await gw.chat()
            assert r.status_code == 200 and r.headers["x-gateway-provider"] == "mock_market"
            assert r.headers["x-gateway-fallback-reason"] == "anthropic:budget_exhausted"
        st = (await gw.status())["providers"]["anthropic"]
    assert st["breaker"]["state"] == "closed" and st["breaker"]["consecutive_failures"] == 0
    assert st["cooldown_remaining_s"] == 0
    assert gw.anthropic_calls == []


async def test_gateway_real_api_blocked_falls_back_without_touching_breaker(tmp_path, monkeypatch):
    monkeypatch.setenv("ENABLE_REAL_API", "0")
    monkeypatch.setenv("ANTHROPIC_API_KEY", fake_key())
    async with gateway(tmp_path) as gw:
        for _ in range(6):
            r = await gw.chat()
            assert r.headers["x-gateway-fallback-reason"] == "anthropic:real_api_blocked"
        st = (await gw.status())["providers"]["anthropic"]
        assert gw.budget.status().used == 0
    assert st["breaker"]["state"] == "closed" and st["breaker"]["consecutive_failures"] == 0
    assert gw.anthropic_calls == []


async def test_gateway_input_too_large_falls_back(tmp_path, real_on):
    big = {"messages": [{"role": "user", "content": "x" * 5000}]}
    async with gateway(tmp_path) as gw:
        r = await gw.chat(big)
        st = (await gw.status())["providers"]["anthropic"]
    assert r.headers["x-gateway-fallback-reason"] == "anthropic:input_too_large"
    assert st["breaker"]["consecutive_failures"] == 0 and gw.budget.status().used == 0


async def test_gateway_forced_anthropic_blocked_leaves_budget_untouched(tmp_path, monkeypatch):
    monkeypatch.setenv("ENABLE_REAL_API", "0")
    monkeypatch.setenv("ANTHROPIC_API_KEY", fake_key())  # chave presente: só a flag pode bloquear
    async with gateway(tmp_path, config=make_config(force=True)) as gw:
        r = await gw.chat(**{"x-gateway-force-provider": "anthropic"})
        assert r.status_code == 503
        assert r.json()["error"]["providers"] == {"anthropic": "real_api_blocked"}
        assert gw.budget.status().used == 0
    assert gw.anthropic_calls == []  # nenhuma conexão foi aberta


async def test_gateway_real_path_end_to_end_spends_one_reservation(tmp_path, real_on):
    async with gateway(tmp_path) as gw:
        r = await gw.chat({**BODY, "max_tokens": 999})
    assert r.status_code == 200 and r.headers["x-gateway-provider"] == "anthropic"
    assert r.json()["choices"][0]["message"]["content"] == "resposta real simulada"
    assert len(gw.anthropic_calls) == 1 and gw.mock_calls == []
    assert json.loads(gw.anthropic_calls[0].content)["max_tokens"] == 50
    assert gw.budget.status().used == 1


async def test_key_never_leaks_through_status_metrics_recorder_or_response(tmp_path, real_on, caplog):
    key = fake_key()
    caplog.set_level(logging.DEBUG)
    cfg = make_config()
    leaky = {"messages": [{"role": "user", "content": f"minha chave e {key}"}]}
    async with gateway(
        tmp_path, cfg, anthropic_handler=lambda r: httpx.Response(500, text=f"erro com {key}")
    ) as gw:
        responses = [await gw.chat(leaky), await gw.chat()]
        texts = [await gw.metrics_text(), json.dumps(await gw.status())]
        texts += [r.text + str(dict(r.headers)) for r in responses]
        # o transporte mock RECEBEU a chave (é assim que a chamada autentica)
        assert gw.anthropic_calls[0].headers["x-api-key"] == key
    log_file = tmp_path / "logs" / "requests.jsonl"
    texts.append(log_file.read_text(encoding="utf-8"))
    for t in texts + [caplog.text]:
        assert key not in t
    assert len(read_jsonl(log_file)) == 2
    status = json.loads(texts[1])
    assert status["real_api_enabled"] is True and "key" not in json.dumps(status).lower()
