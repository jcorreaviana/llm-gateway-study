"""Métricas Prometheus e recorder JSONL, ponta a ponta pelo gateway com mocks."""

import json
import logging
import subprocess
from pathlib import Path

import httpx
import pytest
from prometheus_client.parser import text_string_to_metric_families

from gateway.config import AppConfig
from gateway.recorder import Recorder
from tests.gw_helpers import (
    BODY, MOCK_URL, anthropic_ok_json, fake_key, gateway, make_config, openai_ok, provider_cfg, read_jsonl,
)

ROOT = Path(__file__).resolve().parent.parent


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


def samples(text: str) -> dict[tuple[str, tuple], float]:
    out = {}
    for fam in text_string_to_metric_families(text):
        for s in fam.samples:
            out[(s.name, tuple(sorted(s.labels.items())))] = s.value
    return out


def value(text, name, **labels) -> float:
    return samples(text).get((name, tuple(sorted(labels.items()))), 0.0)


def rate_limited(request):
    return httpx.Response(429, headers={"retry-after": "30"}, json={"error": "rl"})


def server_error(request):
    return httpx.Response(500, json={"error": "boom"})


@pytest.fixture
def real_off(monkeypatch):
    monkeypatch.setenv("ENABLE_REAL_API", "0")


# ------------------------------------------------------------------------- métricas
async def test_metrics_endpoint_is_prometheus_text(tmp_path, real_off):
    async with gateway(tmp_path) as gw:
        r = await gw.client.get("/metrics")
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/plain")
    assert "# TYPE gateway_requests_total counter" in r.text


async def test_success_increments_requests_without_fallback(tmp_path, real_off):
    cfg = make_config(chain=["mock_market"])
    async with gateway(tmp_path, cfg) as gw:
        await gw.chat()
        await gw.chat()
        text = await gw.metrics_text()
    assert value(text, "gateway_requests_total", provider="mock_market", status="200") == 2
    assert not any(n == "gateway_fallback_total" for n, _ in samples(text))
    assert value(text, "gateway_latency_seconds_count", provider="mock_market") == 2


async def test_fallback_labels_include_reason_and_final_provider(tmp_path, real_off):
    async with gateway(tmp_path) as gw:
        await gw.chat()  # anthropic bloqueado -> mock_market
        text = await gw.metrics_text()
    assert value(text, "gateway_fallback_total", from_provider="anthropic",
                 to_provider="mock_market", reason="real_api_blocked") == 1
    assert value(text, "gateway_requests_total", provider="mock_market", status="200") == 1


async def test_503_uses_provider_none_and_to_none(tmp_path, real_off):
    async with gateway(tmp_path, mock_handler=server_error) as gw:
        r = await gw.chat()
        text = await gw.metrics_text()
    assert r.status_code == 503
    assert value(text, "gateway_requests_total", provider="none", status="503") == 1
    assert value(text, "gateway_fallback_total", from_provider="anthropic",
                 to_provider="none", reason="real_api_blocked") == 1
    assert value(text, "gateway_fallback_total", from_provider="mock_market",
                 to_provider="none", reason="server_error") == 1


async def test_rate_limited_fallback_reason(tmp_path, real_off):
    cfg = make_config(chain=["mock_market", "anthropic"])
    async with gateway(tmp_path, cfg, mock_handler=rate_limited) as gw:
        await gw.chat()
        text = await gw.metrics_text()
    assert value(text, "gateway_fallback_total", from_provider="mock_market",
                 to_provider="none", reason="rate_limited") == 1


async def test_client_error_counts_provider_and_status(tmp_path, real_off):
    cfg = make_config(chain=["mock_market"])
    async with gateway(tmp_path, cfg, mock_handler=lambda r: httpx.Response(400, json={"e": 1})) as gw:
        r = await gw.chat()
        text = await gw.metrics_text()
    assert r.status_code == 400
    assert value(text, "gateway_requests_total", provider="mock_market", status="400") == 1


async def test_gateway_400s_count_as_none(tmp_path, real_off):
    async with gateway(tmp_path, make_config(force=True)) as gw:
        await gw.chat({**BODY, "stream": True})
        await gw.chat(**{"x-gateway-force-provider": "ghost"})
        text = await gw.metrics_text()
    assert value(text, "gateway_requests_total", provider="none", status="400") == 2


async def test_latency_only_for_calls_actually_made(tmp_path, real_off):
    async with gateway(tmp_path) as gw:
        await gw.chat()  # anthropic bloqueado (sem chamada); mock respondeu
        text = await gw.metrics_text()
    assert value(text, "gateway_latency_seconds_count", provider="anthropic") == 0
    assert value(text, "gateway_latency_seconds_count", provider="mock_market") == 1


async def test_latency_counts_failed_calls_too(tmp_path, real_off):
    async with gateway(tmp_path, mock_handler=server_error) as gw:
        await gw.chat()
        text = await gw.metrics_text()
    assert value(text, "gateway_latency_seconds_count", provider="mock_market") == 1


async def test_real_api_calls_and_budget_remaining(tmp_path, monkeypatch):
    monkeypatch.setenv("ENABLE_REAL_API", "1")
    monkeypatch.setenv("ANTHROPIC_API_KEY", fake_key())
    async with gateway(tmp_path, budget_limit=5, anthropic_handler=server_error) as gw:
        before = await gw.metrics_text()
        await gw.chat()  # chamada real (simulada) que falha: gasta a reserva mesmo assim
        await gw.chat()
        after = await gw.metrics_text()
    assert value(before, "gateway_budget_remaining") == 5
    assert value(before, "gateway_real_api_calls_total") == 0
    assert value(after, "gateway_real_api_calls_total") == 2
    assert value(after, "gateway_budget_remaining") == 3
    assert value(after, "gateway_latency_seconds_count", provider="anthropic") == 2


async def test_budget_exhausted_does_not_count_as_real_call(tmp_path, monkeypatch):
    monkeypatch.setenv("ENABLE_REAL_API", "1")
    monkeypatch.setenv("ANTHROPIC_API_KEY", fake_key())
    async with gateway(tmp_path, budget_limit=1) as gw:
        gw.budget.reserve()
        await gw.chat()
        text = await gw.metrics_text()
    assert value(text, "gateway_real_api_calls_total") == 0
    assert value(text, "gateway_budget_remaining") == 0
    assert value(text, "gateway_fallback_total", from_provider="anthropic",
                 to_provider="mock_market", reason="budget_exhausted") == 1


async def test_breaker_gauge_reflects_open_and_half_open_by_time(tmp_path, real_off):
    clock = Clock()
    cfg = make_config(chain=["mock_market"], mock_kw={"breaker": {"failure_threshold": 2, "cooldown_s": 30}})
    async with gateway(tmp_path, cfg, mock_handler=server_error, now=clock) as gw:
        t0 = await gw.metrics_text()
        await gw.chat()
        await gw.chat()  # 2ª falha consecutiva abre o breaker
        t1 = await gw.metrics_text()
        clock.t += 31  # só o tempo passou; ninguém chamou o provedor
        t2 = await gw.metrics_text()
    assert value(t0, "gateway_breaker_state", provider="mock_market") == 0
    assert value(t1, "gateway_breaker_state", provider="mock_market") == 2
    assert value(t2, "gateway_breaker_state", provider="mock_market") == 1
    assert value(t0, "gateway_breaker_state", provider="anthropic") == 0


async def test_registry_is_isolated_per_app(tmp_path, real_off):
    cfg = make_config(chain=["mock_market"])
    async with gateway(tmp_path, cfg) as a, gateway(tmp_path, cfg) as b:  # 2 apps: sem registro duplicado
        await a.chat()
        ta, tb = await a.metrics_text(), await b.metrics_text()
        assert a.app.state.metrics.registry is not b.app.state.metrics.registry
    assert value(ta, "gateway_requests_total", provider="mock_market", status="200") == 1
    assert value(tb, "gateway_requests_total", provider="mock_market", status="200") == 0


async def test_labels_have_low_cardinality(tmp_path, real_off):
    secret_prompt = "frase-secreta-do-usuario"
    async with gateway(tmp_path) as gw:
        r = await gw.chat({"model": "modelo-livre-xyz", "messages": [{"role": "user", "content": secret_prompt}]})
        text = await gw.metrics_text()
    assert r.headers["x-gateway-request-id"] not in text
    assert secret_prompt not in text and "modelo-livre-xyz" not in text
    labels = {k for (_, lab) in samples(text) for k, _ in lab}
    assert labels <= {"provider", "status", "from_provider", "to_provider", "reason", "le"}


async def test_status_has_budget_and_boolean_real_api_flag(tmp_path, monkeypatch):
    monkeypatch.setenv("ENABLE_REAL_API", "1")
    monkeypatch.setenv("ANTHROPIC_API_KEY", fake_key())
    async with gateway(tmp_path, budget_limit=7) as gw:
        gw.budget.reserve()
        st = await gw.status()
    assert st["budget"] == {"used": 1, "limit": 7, "remaining": 6}
    assert st["real_api_enabled"] is True
    assert fake_key() not in json.dumps(st)


async def test_status_flag_is_false_by_default_and_reports_corrupt_budget(tmp_path):
    async with gateway(tmp_path) as gw:
        gw.budget.path.parent.mkdir(parents=True, exist_ok=True)
        gw.budget.path.write_text("lixo", encoding="utf-8")
        st = await gw.status()
    assert st["real_api_enabled"] is False
    assert st["budget"]["remaining"] == 0 and "corrompido" in st["budget"]["error"]


# -------------------------------------------------------------------------- recorder
def log_path(tmp_path):
    return tmp_path / "logs" / "requests.jsonl"  # conftest aponta PROJECT_ROOT para tmp_path


async def test_records_one_valid_line_per_request_matching_header(tmp_path, real_off):
    body = {"messages": [{"role": "user", "content": "prompt generico"}], "temperature": 0.2, "max_tokens": 9}
    async with gateway(tmp_path) as gw:
        r1 = await gw.chat(body)
        r2 = await gw.chat()
    lines = read_jsonl(log_path(tmp_path))
    assert len(lines) == 2
    first = lines[0]
    assert first["request_id"] == r1.headers["x-gateway-request-id"] != r2.headers["x-gateway-request-id"]
    assert lines[1]["request_id"] == r2.headers["x-gateway-request-id"]
    assert first["messages"] == body["messages"]
    assert first["temperature"] == 0.2 and first["max_tokens"] == 9
    assert first["provider"] == "mock_market" and first["forced_provider"] is False
    assert first["fallback_reason"] == "anthropic:real_api_blocked"
    assert first["status"] == 200 and first["latency_ms"] >= 0
    assert first["usage"] == {"prompt_tokens": 3, "completion_tokens": 2, "total_tokens": 5}
    assert first["response"] == "ola do mock"
    assert first["timestamp"].endswith("+00:00")


async def test_records_503_and_client_errors(tmp_path, real_off):
    async with gateway(tmp_path, mock_handler=server_error) as gw:
        r = await gw.chat()
    async with gateway(tmp_path, make_config(chain=["mock_market"]),
                       mock_handler=lambda q: httpx.Response(400, json={"e": 1})) as gw:
        r400 = await gw.chat()
        rstream = await gw.chat({**BODY, "stream": True})
    lines = read_jsonl(log_path(tmp_path))
    assert [x["status"] for x in lines] == [503, 400, 400]
    assert lines[0]["provider"] == "none" and lines[0]["response"] is None
    assert lines[0]["fallback_reason"] == "anthropic:real_api_blocked,mock_market:server_error"
    assert lines[1]["provider"] == "mock_market"
    assert lines[2]["provider"] == "none"
    assert [x["request_id"] for x in lines] == [
        r.headers["x-gateway-request-id"], r400.headers["x-gateway-request-id"],
        rstream.headers["x-gateway-request-id"],
    ]


async def test_records_forced_provider(tmp_path, real_off):
    async with gateway(tmp_path, make_config(force=True)) as gw:
        await gw.chat(**{"x-gateway-force-provider": "mock_market"})
    assert read_jsonl(log_path(tmp_path))[0]["forced_provider"] is True


async def test_record_content_false_keeps_only_metadata(tmp_path, real_off):
    cfg = make_config(recorder={"record_content": False})
    async with gateway(tmp_path, cfg) as gw:
        await gw.chat({"messages": [{"role": "user", "content": "segredo-do-usuario"}]})
    raw = log_path(tmp_path).read_text(encoding="utf-8")
    line = json.loads(raw)
    assert "messages" not in line and "response" not in line
    assert "segredo-do-usuario" not in raw and "ola do mock" not in raw
    assert line["provider"] == "mock_market" and line["usage"]["total_tokens"] == 5


async def test_secrets_are_masked_in_recorded_content(tmp_path, monkeypatch):
    monkeypatch.setenv("ENABLE_REAL_API", "0")
    key, env_key = fake_key(), "valor-da-chave-no-ambiente-123"
    monkeypatch.setenv("ANTHROPIC_API_KEY", env_key)

    def echo(request):
        text = json.loads(request.content)["messages"][-1]["content"]
        return httpx.Response(200, json={"model": "m", "choices": [
            {"index": 0, "message": {"role": "assistant", "content": f"eco: {text}"}}]})

    async with gateway(tmp_path, mock_handler=echo) as gw:
        r = await gw.chat({"messages": [{"role": "user", "content": f"a {key} b {env_key} c"}]})
    raw = log_path(tmp_path).read_text(encoding="utf-8")
    assert r.status_code == 200
    assert key not in raw and env_key not in raw and "[REDACTED]" in raw
    assert "a [REDACTED] b [REDACTED] c" in json.loads(raw)["messages"][0]["content"]


async def test_write_failure_does_not_affect_response(tmp_path, real_off, caplog):
    blocker = tmp_path / "bloqueio"
    blocker.write_text("arquivo no lugar do diretorio", encoding="utf-8")
    bad = Recorder(blocker / "requests.jsonl")  # mkdir falha: pai é um arquivo
    caplog.set_level(logging.DEBUG)
    prompt = "prompt-que-nao-pode-ir-ao-log"
    async with gateway(tmp_path, recorder=bad) as gw:
        r = await gw.chat({"messages": [{"role": "user", "content": prompt}]})
    assert r.status_code == 200 and r.headers["x-gateway-provider"] == "mock_market"
    assert r.headers["x-gateway-request-id"]
    assert "recorder: falha ao gravar" in caplog.text
    assert prompt not in caplog.text and "ola do mock" not in caplog.text


def test_recorder_write_never_raises_even_on_unserializable(tmp_path):
    rec = Recorder(tmp_path / "x" / "r.jsonl")
    rec.write({"x": object()})  # json.dumps falha: só avisa
    assert not (tmp_path / "x" / "r.jsonl").exists()


def test_recorder_appends_one_line_each(tmp_path):
    rec = Recorder(tmp_path / "a" / "r.jsonl")
    for i in range(3):
        rec.write({"i": i, "texto": "açaí\ncom quebra"})
    lines = (tmp_path / "a" / "r.jsonl").read_text(encoding="utf-8").splitlines()
    assert [json.loads(x)["i"] for x in lines] == [0, 1, 2]


async def test_app_logs_still_have_no_prompt_response_or_key(tmp_path, monkeypatch, caplog):
    monkeypatch.setenv("ENABLE_REAL_API", "1")
    monkeypatch.setenv("ANTHROPIC_API_KEY", fake_key())
    caplog.set_level(logging.DEBUG)
    prompt = "frase-secreta-do-usuario"
    async with gateway(tmp_path) as gw:
        await gw.chat({"messages": [{"role": "user", "content": prompt}]})
    assert prompt not in caplog.text and "resposta real simulada" not in caplog.text
    assert fake_key() not in caplog.text and "request_id=" in caplog.text


# ---------------------------------------------------------------------------- git ignore
@pytest.mark.parametrize("path", ["logs/requests.jsonl", "data/budget.json", "data/budget.json.lock"])
def test_runtime_files_are_git_ignored(path):
    r = subprocess.run(["git", "check-ignore", "-q", path], cwd=ROOT)
    assert r.returncode == 0, f"{path} deveria estar no .gitignore"
