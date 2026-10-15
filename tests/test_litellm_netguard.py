"""Testes da trava de saída de rede do smoke do LiteLLM (litellm/netguard/sitecustomize.py).

Cada caso roda um subprocesso Python com o PYTHONPATH da trava. Só IPs literais e portas locais; nada
sai da máquina (TEST-NET-1 e `.invalid` são bloqueados ANTES da chamada de sistema, sem pacote nem DNS).
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

NETGUARD_DIR = Path(__file__).resolve().parent.parent / "litellm" / "netguard"
EXTERNAL_IP = "192.0.2.1"  # TEST-NET-1 (RFC 5737): reservado para documentação, nunca roteado


def run_guarded(code: str, tmp_path: Path, **extra_env: str) -> tuple[subprocess.CompletedProcess, list[dict]]:
    log = tmp_path / "netguard.jsonl"
    env = {k: v for k, v in os.environ.items() if k not in ("PYTHONPATH", "PYTHONHOME")}
    env.update(PYTHONPATH=str(NETGUARD_DIR), NETGUARD_LOG=str(log), **extra_env)
    proc = subprocess.run([sys.executable, "-c", textwrap.dedent(code)], env=env, capture_output=True,
                          text=True, timeout=60)
    rows = [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()] if log.exists() else []
    return proc, rows


def blocked(rows: list[dict]) -> list[dict]:
    return [r for r in rows if r["decision"].startswith("BLOCKED")]


def test_hook_is_armed_in_child(tmp_path):
    proc, rows = run_guarded("print('ok')", tmp_path)
    assert proc.returncode == 0, proc.stderr
    assert [r["decision"] for r in rows] == ["armed"]


def test_connect_to_external_ip_is_blocked_and_logged(tmp_path):
    code = f"""
        import socket
        from sitecustomize import NetguardViolation
        s = socket.socket()
        try:
            s.connect(("{EXTERNAL_IP}", 9))
        except NetguardViolation:
            print("BLOQUEADO")
    """
    proc, rows = run_guarded(code, tmp_path)
    assert "BLOQUEADO" in proc.stdout, proc.stderr
    assert len(blocked(rows)) == 1
    assert blocked(rows)[0]["event"] == "socket.connect"
    assert EXTERNAL_IP in blocked(rows)[0]["target"]


def test_name_resolution_of_external_host_is_blocked_before_any_lookup(tmp_path):
    code = """
        import socket
        from sitecustomize import NetguardViolation
        for call in (lambda: socket.getaddrinfo("exemplo.invalid", 443),
                     lambda: socket.gethostbyname("exemplo.invalid")):
            try:
                call()
            except NetguardViolation:
                print("BLOQUEADO")
    """
    proc, rows = run_guarded(code, tmp_path)
    assert proc.stdout.count("BLOQUEADO") == 2, proc.stderr
    assert {r["event"] for r in blocked(rows)} == {"socket.getaddrinfo", "socket.gethostbyname"}


def test_connect_to_loopback_server_works(tmp_path):
    with socket.socket() as server:
        server.bind(("127.0.0.1", 0))
        server.listen(1)
        port = server.getsockname()[1]
        code = f"""
            import socket
            with socket.create_connection(("127.0.0.1", {port}), timeout=5) as c:
                print("CONECTOU")
            print(socket.getaddrinfo("localhost", {port})[0][0] is not None)
        """
        proc, rows = run_guarded(code, tmp_path)
    assert "CONECTOU" in proc.stdout, proc.stderr
    assert blocked(rows) == []
    assert any(r["event"] == "socket.connect" and r["decision"] == "allowed" for r in rows)


def test_asyncio_connect_to_external_ip_is_blocked(tmp_path):
    code = f"""
        import asyncio
        from sitecustomize import NetguardViolation
        async def main():
            try:
                await asyncio.open_connection("{EXTERNAL_IP}", 9)
            except NetguardViolation:
                print("BLOQUEADO")
        asyncio.run(main())
    """
    proc, rows = run_guarded(code, tmp_path)
    assert "BLOQUEADO" in proc.stdout, proc.stderr
    assert any(r["event"] == "asyncio.create_connection" for r in blocked(rows))


def test_asyncio_connect_to_loopback_server_works(tmp_path):
    with socket.socket() as server:
        server.bind(("127.0.0.1", 0))
        server.listen(1)
        port = server.getsockname()[1]
        code = f"""
            import asyncio
            async def main():
                r, w = await asyncio.open_connection("127.0.0.1", {port})
                w.close()
                print("CONECTOU")
            asyncio.run(main())
        """
        proc, rows = run_guarded(code, tmp_path)
    assert "CONECTOU" in proc.stdout, proc.stderr
    assert blocked(rows) == []


def test_connection_to_sink_port_is_blocked_even_on_loopback(tmp_path):
    with socket.socket() as sink:
        sink.bind(("127.0.0.1", 0))
        sink.listen(1)
        port = sink.getsockname()[1]
        code = f"""
            import socket
            from sitecustomize import NetguardViolation
            try:
                socket.create_connection(("127.0.0.1", {port}), timeout=5)
            except NetguardViolation:
                print("BLOQUEADO")
        """
        proc, rows = run_guarded(code, tmp_path, NETGUARD_SINK_PORT=str(port))
    assert "BLOQUEADO" in proc.stdout, proc.stderr
    assert [r["decision"] for r in blocked(rows)] == ["BLOCKED_SINK"]


def test_violation_is_not_an_oserror(tmp_path):
    code = """
        from sitecustomize import NetguardViolation
        print(issubclass(NetguardViolation, OSError))
    """
    proc, _ = run_guarded(code, tmp_path)
    assert proc.stdout.strip() == "False", proc.stderr


def test_pythonpath_reaches_grandchild_process(tmp_path):
    code = f"""
        import subprocess, sys
        child = "import socket\\ntry:\\n    socket.create_connection(('{EXTERNAL_IP}', 9))\\nexcept Exception as e:\\n    print(type(e).__name__)"
        print(subprocess.run([sys.executable, "-c", child], capture_output=True, text=True).stdout.strip())
    """
    proc, rows = run_guarded(code, tmp_path)
    assert proc.stdout.strip() == "NetguardViolation", proc.stderr
    assert len({r["pid"] for r in rows if r["decision"] == "armed"}) == 2
    assert len(blocked(rows)) == 1


@pytest.mark.parametrize("host,expected", [("127.0.0.1", True), ("::1", True), ("localhost", True),
                                           (EXTERNAL_IP, False), ("exemplo.invalid", False), ("", False)])
def test_loopback_classifier(host, expected, tmp_path):
    # em subprocesso: importar a trava aqui armaria o audit hook no próprio processo do pytest
    proc, _ = run_guarded(f"import sitecustomize; print(sitecustomize._is_loopback({host!r}))", tmp_path)
    assert proc.stdout.strip() == str(expected), proc.stderr
