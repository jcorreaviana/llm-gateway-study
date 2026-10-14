"""Testes de integração (desligados por padrão: `pytest -m integration`).

Precisam do Ollama real em localhost:11434 com `qwen2.5:3b`; sem ele, `skip` com mensagem clara.
Sobem o mock e o gateway como PROCESSOS reais (pontos de entrada de verdade), em portas livres
escolhidas na hora, e encerram tudo no fim, inclusive se um teste falhar. Nada fala com a
Anthropic: o provedor `anthropic` fica desabilitado e `ENABLE_REAL_API=0` no ambiente dos filhos.
"""

import contextlib
import socket
from pathlib import Path

import httpx
import pytest
import yaml

from scripts._stack import Stack, free_ports, start_stack

pytestmark = pytest.mark.integration

OLLAMA_URL = "http://localhost:11434"
MODEL = "qwen2.5:3b"
BODY = {"messages": [{"role": "user", "content": "Say hello in one word."}], "max_tokens": 8}


def port_is_open(port: int) -> bool:
    with socket.socket() as s:
        s.settimeout(0.5)
        return s.connect_ex(("127.0.0.1", port)) == 0


@pytest.fixture(scope="module")
def ollama():
    try:
        tags = httpx.get(f"{OLLAMA_URL}/api/tags", timeout=3).json()
    except (httpx.HTTPError, ValueError) as e:
        pytest.skip(f"Ollama indisponível em {OLLAMA_URL} ({type(e).__name__}): suba o Ollama para rodar -m integration")
    names = {m.get("name") for m in tags.get("models", [])}
    if MODEL not in names:
        pytest.skip(f"modelo {MODEL} não instalado no Ollama (rode: ollama pull {MODEL})")


def write_config(tmp_path: Path, gateway_port: int, mock_port: int) -> Path:
    cfg = {
        "gateway": {"port": gateway_port, "force_provider_enabled": True},
        "mock_upstream": {"port": mock_port},
        "budget": {"limit": 30, "path": str(tmp_path / "data" / "budget.json")},
        "recorder": {"path": str(tmp_path / "logs" / "requests.jsonl"), "record_content": True},
        "chain": ["mock_market", "ollama_qwen"],
        "providers": [
            {"name": "mock_market", "type": "mock", "base_url": f"http://127.0.0.1:{mock_port}",
             "model": "mock-market-1", "timeout_s": 10, "context_window": 8192, "enabled": True},
            {"name": "anthropic", "type": "anthropic", "model": "claude-haiku-5-5", "timeout_s": 30,
             "context_window": 200000, "enabled": False},
            {"name": "ollama_qwen", "type": "ollama", "base_url": OLLAMA_URL, "model": MODEL,
             "timeout_s": 120, "context_window": 3072, "enabled": True},
        ],
    }
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(cfg), encoding="utf-8")
    return path


def chat(stack: Stack, body=BODY, **headers) -> httpx.Response:
    return httpx.post(f"{stack.gateway_url}/v1/chat/completions", json=body, headers=headers, timeout=180)


@contextlib.contextmanager
def running_stack(tmp_path: Path):
    """Mock e gateway como processos reais (scripts/_stack.py); encerra ambos em qualquer caso."""
    gateway_port, mock_port = free_ports()
    config = write_config(tmp_path, gateway_port, mock_port)
    with start_stack(config, tmp_path, gateway_port, mock_port) as s:
        yield s


@pytest.fixture
def stack(ollama, tmp_path):
    """Um stack novo por teste: cooldown, breaker e budget do gateway são estado de processo, então
    compartilhar o stack criaria dependência de ordem entre os testes."""
    with running_stack(tmp_path) as s:
        yield s


def test_real_ollama_qwen_through_gateway_forced(stack):
    r = chat(stack, **{"x-gateway-force-provider": "ollama_qwen"})
    assert r.status_code == 200, r.text
    assert r.headers["x-gateway-provider"] == "ollama_qwen"
    assert "x-gateway-fallback-reason" not in r.headers
    data = r.json()
    assert data["model"].startswith("qwen2.5")
    message = data["choices"][0]["message"]
    assert message["role"] == "assistant" and isinstance(message["content"], str)
    assert data["usage"]["total_tokens"] > 0


def test_real_fallback_mock_rate_limit_to_ollama(stack):
    httpx.post(f"{stack.mock_url}/chaos", json={"mode": "rate_limit", "retry_after_seconds": 5}, timeout=5)
    r = chat(stack)
    assert r.status_code == 200, r.text
    assert r.headers["x-gateway-provider"] == "ollama_qwen"
    assert r.headers["x-gateway-fallback-reason"] == "mock_market:rate_limited"
    assert r.json()["choices"][0]["message"]["content"] is not None
    stats = httpx.get(f"{stack.mock_url}/stats", timeout=5).json()
    assert stats["received"] == 1 and stats["by_status"] == {"429": 1}


def test_real_processes_end_to_end(stack):
    """Pontos de entrada reais: `python -m mock_upstream.server` e uvicorn do gateway."""
    r = chat(stack)
    assert r.status_code == 200
    assert r.headers["x-gateway-provider"] == "mock_market"
    assert r.json()["choices"][0]["message"]["content"].startswith("[mock]")
    assert httpx.get(f"{stack.mock_url}/stats", timeout=5).json()["received"] == 1
    status = httpx.get(f"{stack.gateway_url}/status", timeout=5).json()
    assert status["real_api_enabled"] is False
    assert status["providers"]["mock_market"]["breaker"]["state"] == "closed"
    assert "gateway_requests_total" in httpx.get(f"{stack.gateway_url}/metrics", timeout=5).text
    # a Anthropic continua fail-closed nos processos reais
    blocked = chat(stack, **{"x-gateway-force-provider": "anthropic"})
    assert blocked.status_code == 503
    assert blocked.json()["error"]["providers"] == {"anthropic": "disabled"}


def test_processes_are_terminated_when_the_context_exits(ollama, tmp_path):
    with running_stack(tmp_path) as s:
        procs, ports = list(s.procs), (s.mock_port, s.gateway_port)
        assert all(p.poll() is None for p in procs) and all(port_is_open(p) for p in ports)
    assert all(p.poll() is not None for p in procs), "processo órfão"
    assert not any(port_is_open(p) for p in ports)


def test_processes_are_terminated_even_when_the_body_fails(ollama, tmp_path):
    procs = []
    with pytest.raises(RuntimeError, match="falha de propósito"):
        with running_stack(tmp_path) as s:
            procs = list(s.procs)
            raise RuntimeError("falha de propósito")
    assert procs and all(p.poll() is not None for p in procs), "processo órfão após falha"
