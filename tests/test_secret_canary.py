"""Canário de segredo, ponta a ponta: uma chave falsa, montada em runtime, entra como
ANTHROPIC_API_KEY e cenários completos rodam. Depois TODAS as saídas são varridas: logs de
aplicação, corpos e headers de resposta, /status, /metrics, o arquivo do recorder e as mensagens
de exceção. O canário não pode aparecer em nenhuma.

Para ser um teste duro, os provedores "malvados" ecoam a chave nos corpos de erro, como um
upstream com bug faria."""

import json
import logging

import httpx
import pytest

from gateway.providers import AnthropicProvider, ProviderFailure
from gateway.router import AllProvidersFailed
from tests.gw_helpers import BODY, anthropic_ok_json, gateway, make_config, read_jsonl

CANARY = "sk-ant-" + "canary" * 6  # montada em runtime; o código-fonte não tem nada com cara de chave
PROMPT = "prompt-secreto-do-usuario-9137"
USER_BODY = {"messages": [{"role": "user", "content": PROMPT}], "max_tokens": 30}


def echo_error(status: int, headers: dict | None = None):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            status, headers=headers, json={"error": {"message": f"falha {status} com {CANARY}"}}
        )

    return handler


def raise_timeout(request: httpx.Request):
    raise httpx.ReadTimeout(f"timeout vazando {CANARY}")


def ok(request: httpx.Request) -> httpx.Response:
    return httpx.Response(200, json=anthropic_ok_json())


# nome -> (handler do anthropic, handler do mock, budget_limit, status HTTP esperado do gateway)
SCENARIOS = {
    "success": (ok, None, 30, 200),
    "429": (echo_error(429, {"Retry-After": "7"}), None, 30, 200),
    "500": (echo_error(500), None, 30, 200),
    "401": (echo_error(401), None, 30, 200),
    "timeout": (raise_timeout, None, 30, 200),
    "client_error_400": (echo_error(400), None, 30, 400),
    "all_fail_503": (echo_error(500), echo_error(500), 30, 503),
    "budget_exhausted": (ok, None, 1, 200),
}


async def collect_outputs(gw, responses, caplog, tmp_path) -> dict[str, str]:
    outputs = {"caplog": caplog.text}
    for i, r in enumerate(responses):
        outputs[f"response[{i}].body"] = r.text
        outputs[f"response[{i}].headers"] = json.dumps(dict(r.headers))
    outputs["/status"] = (await gw.client.get("/status")).text
    outputs["/metrics"] = await gw.metrics_text()
    log_file = tmp_path / "logs" / "requests.jsonl"
    outputs["recorder"] = log_file.read_text(encoding="utf-8") if log_file.exists() else ""
    return outputs


def assert_no_canary(outputs: dict[str, str]) -> None:
    for where, text in outputs.items():
        assert CANARY not in text, f"canário vazou em {where}"
        assert "canarycanary" not in text, f"pedaço do canário vazou em {where}"


@pytest.mark.parametrize("name", list(SCENARIOS))
async def test_canary_never_appears_in_any_output(name, tmp_path, monkeypatch, caplog):
    anthropic_handler, mock_handler, limit, expected_status = SCENARIOS[name]
    monkeypatch.setenv("ENABLE_REAL_API", "1")
    monkeypatch.setenv("ANTHROPIC_API_KEY", CANARY)
    caplog.set_level(logging.DEBUG)
    kwargs = {"mock_handler": mock_handler} if mock_handler else {}
    async with gateway(
        tmp_path, anthropic_handler=anthropic_handler, budget_limit=limit, **kwargs
    ) as gw:
        if name == "budget_exhausted":
            assert gw.budget.reserve()  # esgota o único crédito
        responses = [await gw.chat(USER_BODY), await gw.chat(USER_BODY)]
        assert responses[0].status_code == expected_status, responses[0].text
        outputs = await collect_outputs(gw, responses, caplog, tmp_path)
        if name == "success":  # a chave AUTENTICA a chamada real (mockada), mas só aí
            assert gw.anthropic_calls[0].headers["x-api-key"] == CANARY
    assert_no_canary(outputs)
    assert len(read_jsonl(tmp_path / "logs" / "requests.jsonl")) == 2


async def test_canary_not_in_exception_messages_or_failure_objects(tmp_path, monkeypatch):
    """Mensagens de exceção e objetos de falha (o que o router e os logs poderiam formatar)."""
    monkeypatch.setenv("ENABLE_REAL_API", "1")
    monkeypatch.setenv("ANTHROPIC_API_KEY", CANARY)
    cfg = make_config(chain=["anthropic", "mock_market"], mock_kw={"breaker": {"failure_threshold": 99}})
    async with gateway(
        tmp_path, cfg, anthropic_handler=echo_error(500), mock_handler=echo_error(500)
    ) as gw:
        router = gw.app.state.router
        with pytest.raises(AllProvidersFailed) as exc:
            await router.route(dict(BODY), "rid-1")
        texts = [str(exc.value), repr(exc.value), repr(exc.value.attempts)]
        provider = router.providers["anthropic"]
        assert isinstance(provider, AnthropicProvider)
        failure = await provider.complete(dict(BODY))
        assert isinstance(failure, ProviderFailure)
        texts += [repr(failure), str(failure)]
    for t in texts:
        assert CANARY not in t


async def test_secret_in_user_prompt_is_masked_by_recorder(tmp_path, monkeypatch):
    """Uma chave digitada pelo usuário NO PROMPT (inclusive o canário e outra chave qualquer)
    não chega ao arquivo do recorder, nem na ida (messages) nem na volta (response ecoada)."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", CANARY)
    other = "sk-" + "outra" * 6

    def echo(request: httpx.Request) -> httpx.Response:
        text = json.loads(request.content)["messages"][-1]["content"]
        return httpx.Response(200, json={"model": "m", "choices": [
            {"index": 0, "message": {"role": "assistant", "content": f"eco: {text}"}}]})

    body = {"messages": [{"role": "user", "content": f"use {CANARY} ou {other} por favor"}]}
    async with gateway(tmp_path, mock_handler=echo) as gw:
        r = await gw.chat(body)
    assert r.status_code == 200
    raw = (tmp_path / "logs" / "requests.jsonl").read_text(encoding="utf-8")
    assert CANARY not in raw and other not in raw
    entry = json.loads(raw)
    assert entry["messages"][0]["content"] == "use [REDACTED] ou [REDACTED] por favor"
    assert entry["response"] == "eco: use [REDACTED] ou [REDACTED] por favor"


async def test_status_and_metrics_expose_no_prompt_or_response(tmp_path, monkeypatch, caplog):
    monkeypatch.setenv("ENABLE_REAL_API", "1")
    monkeypatch.setenv("ANTHROPIC_API_KEY", CANARY)
    caplog.set_level(logging.DEBUG)
    reply = "resposta-secreta-do-modelo-4471"

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"model": "m", "choices": [
            {"index": 0, "message": {"role": "assistant", "content": reply}}]})

    async with gateway(tmp_path, anthropic_handler=echo_error(500), mock_handler=handler) as gw:
        r = await gw.chat(USER_BODY)
        assert r.status_code == 200 and reply in r.text  # o CLIENTE recebe a resposta
        status = (await gw.client.get("/status")).text
        metrics = await gw.metrics_text()
    for name, text in {"/status": status, "/metrics": metrics, "caplog": caplog.text}.items():
        assert PROMPT not in text, f"prompt vazou em {name}"
        assert reply not in text, f"resposta vazou em {name}"
    # o recorder (local, ignorado pelo git) é o ÚNICO lugar com conteúdo, por desenho
    raw = (tmp_path / "logs" / "requests.jsonl").read_text(encoding="utf-8")
    assert PROMPT in raw and reply in raw
