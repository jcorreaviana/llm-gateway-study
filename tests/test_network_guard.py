"""A fixture autouse de bloqueio de rede (tests/netguard.py): conexão externa deve falhar."""

import asyncio
import socket

import httpx
import pytest

from tests.netguard import ExternalNetworkBlocked, NetworkGuard, is_local

# IP de documentação (RFC 5737): nunca roteável, então mesmo um furo na fixture não sai daqui.
EXTERNAL_IP = "203.0.113.7"


@pytest.mark.parametrize(
    "host", ["127.0.0.1", "localhost", "LOCALHOST", "::1", "[::1]", "127.1.2.3", None]
)
def test_loopback_is_allowed(host):
    assert is_local(host)


@pytest.mark.parametrize(
    "host", [EXTERNAL_IP, "api.anthropic.com", "example.com", "8.8.8.8", "localhost.evil.com"]
)
def test_everything_else_is_blocked(host):
    assert not is_local(host)


@pytest.fixture
def guard(_block_external_network):
    """Usa o guard autouse, mas limpa as violações esperadas para o teste não falhar no fim."""
    yield _block_external_network
    _block_external_network.violations.clear()


def test_sync_socket_connect_to_external_ip_fails(guard):
    s = socket.socket()
    try:
        with pytest.raises(ExternalNetworkBlocked):
            s.connect((EXTERNAL_IP, 80))
    finally:
        s.close()
    assert guard.violations


def test_sync_name_resolution_of_external_host_fails(guard):
    with pytest.raises(ExternalNetworkBlocked):
        socket.getaddrinfo("api.anthropic.com", 443)
    with pytest.raises(ExternalNetworkBlocked):
        socket.create_connection(("api.anthropic.com", 443), timeout=1)


async def test_asyncio_connection_to_external_host_fails(guard):
    with pytest.raises(ExternalNetworkBlocked):
        await asyncio.open_connection(EXTERNAL_IP, 80)
    with pytest.raises(ExternalNetworkBlocked):
        await asyncio.open_connection("api.anthropic.com", 443)


async def test_real_httpx_request_to_anthropic_fails(guard):
    async with httpx.AsyncClient(timeout=2) as c:
        with pytest.raises(Exception):  # noqa: B017 - o tipo depende da camada do httpx
            await c.post("https://api.anthropic.com/v1/messages", json={})
    assert guard.violations


async def test_loopback_still_works():
    async def handler(reader, writer):
        writer.close()

    server = await asyncio.start_server(handler, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    async with server:
        _, writer = await asyncio.open_connection("127.0.0.1", port)
        writer.close()
    with socket.socket() as s:  # connect síncrono para loopback também passa pelo guard
        s.settimeout(1)
        with pytest.raises(OSError):  # porta fechada: erro normal de rede, não do bloqueio
            s.connect(("127.0.0.1", 1))


def test_install_and_uninstall_restore_socket():
    original = socket.socket.connect
    g = NetworkGuard()
    g.install()
    assert socket.socket.connect is not original
    g.uninstall()
    assert socket.socket.connect is original
