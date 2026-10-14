"""Pilha de processos compartilhada: mock e gateway como processos reais, em 127.0.0.1 e portas livres.

Usada pela demo (`scripts/demo.py`), pelos testes de integração e pelo runner de avaliação
(`evalkit.run`). Garante o encerramento dos dois processos em qualquer caso (erro, Ctrl+C) e sobe os
filhos com `ENABLE_REAL_API=0` e `ANTHROPIC_API_KEY` vazia: este módulo nunca fala com a Anthropic.
"""

from __future__ import annotations

import contextlib
import os
import socket
import subprocess
import sys
import time
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

import httpx

REPO_ROOT = Path(__file__).resolve().parent.parent
MOCK_HEALTH_TIMEOUT_S = 40
GATEWAY_HEALTH_TIMEOUT_S = 240  # o warm-up dos modelos locais roda antes do gateway aceitar conexões


class StackError(Exception):
    """Falha esperada ao subir a pilha: mensagem clara, sem traceback."""


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def free_ports() -> tuple[int, int]:
    """(porta do gateway, porta do mock), sempre diferentes."""
    gateway_port, mock_port = free_port(), free_port()
    while mock_port == gateway_port:
        mock_port = free_port()
    return gateway_port, mock_port


def child_env(base: dict[str, str] | None = None, **extra: str) -> dict[str, str]:
    """Ambiente dos processos filhos: API real desligada e nenhuma chave. `ANTHROPIC_API_KEY` vai
    VAZIA (não ausente): com a variável ausente, o load_dotenv do gateway leria a chave de um `.env`
    local; uma variável já definida nunca é sobrescrita por ele."""
    env = dict(os.environ if base is None else base)
    env.update(ENABLE_REAL_API="0", ANTHROPIC_API_KEY="", PYTHONUNBUFFERED="1", PYTHONIOENCODING="utf-8")
    env.update(extra)
    return env


def stop(proc: subprocess.Popen) -> None:
    if proc.poll() is None:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=10)


def wait_health(url: str, proc: subprocess.Popen, log: Path, timeout_s: float) -> None:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            tail = log.read_text(encoding="utf-8", errors="replace")[-1500:]
            raise StackError(f"processo encerrou antes do /health (codigo {proc.returncode}):\n{tail}")
        try:
            if httpx.get(f"{url}/health", timeout=1).status_code == 200:
                return
        except httpx.HTTPError:
            time.sleep(0.2)
    raise StackError(f"{url}/health nao respondeu em {timeout_s:.0f}s")


@dataclass
class Stack:
    mock_url: str
    gateway_url: str
    procs: list[subprocess.Popen] = field(default_factory=list)

    @property
    def mock_port(self) -> int:
        return urlsplit(self.mock_url).port or 0

    @property
    def gateway_port(self) -> int:
        return urlsplit(self.gateway_url).port or 0


@contextlib.contextmanager
def start_stack(config_path: Path, workdir: Path, gateway_port: int, mock_port: int) -> Iterator[Stack]:
    """Mock e gateway como processos reais, em 127.0.0.1; encerra ambos em qualquer caso (erro ou Ctrl+C).
    Os logs dos processos ficam em `workdir/mock.log` e `workdir/gateway.log`."""
    env = child_env(GATEWAY_CONFIG=str(config_path), MOCK_UPSTREAM_PORT=str(mock_port))
    flags = subprocess.CREATE_NEW_PROCESS_GROUP if sys.platform == "win32" else 0  # Ctrl+C só no pai
    logs = {n: workdir / f"{n}.log" for n in ("mock", "gateway")}
    handles = [open(p, "wb") for p in logs.values()]
    stack = Stack(f"http://127.0.0.1:{mock_port}", f"http://127.0.0.1:{gateway_port}")
    try:
        stack.procs.append(subprocess.Popen(
            [sys.executable, "-m", "mock_upstream.server"],
            cwd=REPO_ROOT, env=env, stdout=handles[0], stderr=subprocess.STDOUT, creationflags=flags))
        stack.procs.append(subprocess.Popen(
            [sys.executable, "-m", "uvicorn", "gateway.main:app_factory", "--factory",
             "--host", "127.0.0.1", "--port", str(gateway_port), "--log-level", "warning"],
            cwd=REPO_ROOT, env=env, stdout=handles[1], stderr=subprocess.STDOUT, creationflags=flags))
        wait_health(stack.mock_url, stack.procs[0], logs["mock"], MOCK_HEALTH_TIMEOUT_S)
        wait_health(stack.gateway_url, stack.procs[1], logs["gateway"], GATEWAY_HEALTH_TIMEOUT_S)
        yield stack
    finally:
        for p in stack.procs:
            stop(p)
        for h in handles:
            h.close()
