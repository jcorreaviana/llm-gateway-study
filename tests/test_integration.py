"""Testes de integração (desligados por padrão: `pytest -m integration`).

Precisam do Ollama real em localhost:11434 com `qwen2.5:3b`; sem ele, `skip` com mensagem clara.
Sobem o mock e o gateway como PROCESSOS reais (pontos de entrada de verdade), em portas livres
escolhidas na hora, e encerram tudo no fim, inclusive se um teste falhar. Nada fala com a
Anthropic: o provedor `anthropic` fica desabilitado e `ENABLE_REAL_API=0` no ambiente dos filhos.
"""

import contextlib
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

import httpx
import pytest
import yaml

pytestmark = pytest.mark.integration

REPO_ROOT = Path(__file__).resolve().parent.parent
OLLAMA_URL = "http://localhost:11434"
MODEL = "qwen2.5:3b"
BODY = {"messages": [{"role": "user", "content": "Say hello in one word."}], "max_tokens": 8}


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


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


class Stack:
    def __init__(self, mock_port: int, gateway_port: int, procs: list[subprocess.Popen]):
        self.mock_port, self.gateway_port, self.procs = mock_port, gateway_port, procs
        self.mock = f"http://127.0.0.1:{mock_port}"
        self.gateway = f"http://127.0.0.1:{gateway_port}"

    def chat(self, body=BODY, **headers) -> httpx.Response:
        return httpx.post(f"{self.gateway}/v1/chat/completions", json=body, headers=headers, timeout=180)


def wait_health(url: str, proc: subprocess.Popen, log: Path, timeout_s: float = 40) -> None:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            pytest.fail(f"processo encerrou antes do /health (código {proc.returncode}):\n"
                        f"{log.read_text(encoding='utf-8', errors='replace')[-1500:]}")
        try:
            if httpx.get(f"{url}/health", timeout=1).status_code == 200:
                return
        except httpx.HTTPError:
            time.sleep(0.2)
    pytest.fail(f"{url}/health não respondeu em {timeout_s}s")


def stop(proc: subprocess.Popen) -> None:
    if proc.poll() is None:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=10)


@contextlib.contextmanager
def running_stack(tmp_path: Path):
    """Mock e gateway como processos reais; garante o encerramento de ambos em qualquer caso."""
    mock_port, gateway_port = free_port(), free_port()
    while gateway_port == mock_port:
        gateway_port = free_port()
    config = write_config(tmp_path, gateway_port, mock_port)
    env = {k: v for k, v in os.environ.items() if k not in ("ANTHROPIC_API_KEY",)}
    env.update(
        ENABLE_REAL_API="0",
        ANTHROPIC_API_KEY="",  # vazio (não ausente): load_dotenv não sobrescreve variável existente
        GATEWAY_CONFIG=str(config),
        MOCK_UPSTREAM_PORT=str(mock_port),
        PYTHONUNBUFFERED="1",
    )
    procs: list[subprocess.Popen] = []
    logs = {n: tmp_path / f"{n}.log" for n in ("mock", "gateway")}
    handles = [open(p, "wb") for p in logs.values()]
    try:
        procs.append(subprocess.Popen(
            [sys.executable, "-m", "mock_upstream.server"],
            cwd=REPO_ROOT, env=env, stdout=handles[0], stderr=subprocess.STDOUT))
        procs.append(subprocess.Popen(
            [sys.executable, "-m", "uvicorn", "gateway.main:app_factory", "--factory",
             "--host", "127.0.0.1", "--port", str(gateway_port), "--log-level", "warning"],
            cwd=REPO_ROOT, env=env, stdout=handles[1], stderr=subprocess.STDOUT))
        wait_health(f"http://127.0.0.1:{mock_port}", procs[0], logs["mock"])
        wait_health(f"http://127.0.0.1:{gateway_port}", procs[1], logs["gateway"])
        yield Stack(mock_port, gateway_port, procs)
    finally:
        for p in procs:
            stop(p)
        for h in handles:
            h.close()


@pytest.fixture
def stack(ollama, tmp_path):
    """Um stack novo por teste: cooldown, breaker e budget do gateway são estado de processo, então
    compartilhar o stack criaria dependência de ordem entre os testes."""
    with running_stack(tmp_path) as s:
        yield s


def test_real_ollama_qwen_through_gateway_forced(stack):
    r = stack.chat(**{"x-gateway-force-provider": "ollama_qwen"})
    assert r.status_code == 200, r.text
    assert r.headers["x-gateway-provider"] == "ollama_qwen"
    assert "x-gateway-fallback-reason" not in r.headers
    data = r.json()
    assert data["model"].startswith("qwen2.5")
    message = data["choices"][0]["message"]
    assert message["role"] == "assistant" and isinstance(message["content"], str)
    assert data["usage"]["total_tokens"] > 0


def test_real_fallback_mock_rate_limit_to_ollama(stack):
    httpx.post(f"{stack.mock}/chaos", json={"mode": "rate_limit", "retry_after_seconds": 5}, timeout=5)
    r = stack.chat()
    assert r.status_code == 200, r.text
    assert r.headers["x-gateway-provider"] == "ollama_qwen"
    assert r.headers["x-gateway-fallback-reason"] == "mock_market:rate_limited"
    assert r.json()["choices"][0]["message"]["content"] is not None
    stats = httpx.get(f"{stack.mock}/stats", timeout=5).json()
    assert stats["received"] == 1 and stats["by_status"] == {"429": 1}


def test_real_processes_end_to_end(stack):
    """Pontos de entrada reais: `python -m mock_upstream.server` e uvicorn do gateway."""
    r = stack.chat()
    assert r.status_code == 200
    assert r.headers["x-gateway-provider"] == "mock_market"
    assert r.json()["choices"][0]["message"]["content"].startswith("[mock]")
    assert httpx.get(f"{stack.mock}/stats", timeout=5).json()["received"] == 1
    status = httpx.get(f"{stack.gateway}/status", timeout=5).json()
    assert status["real_api_enabled"] is False
    assert status["providers"]["mock_market"]["breaker"]["state"] == "closed"
    assert "gateway_requests_total" in httpx.get(f"{stack.gateway}/metrics", timeout=5).text
    # a Anthropic continua fail-closed nos processos reais
    blocked = stack.chat(**{"x-gateway-force-provider": "anthropic"})
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
