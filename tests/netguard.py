"""Bloqueio de rede para a suíte: só 127.0.0.1, localhost e ::1.

Cobre `socket.connect/connect_ex`, a resolução de nomes (`getaddrinfo`) e as portas de entrada do
asyncio (`create_connection`, `sock_connect`), que no Windows (Proactor) não passam por
`socket.connect`. Toda tentativa proibida é registrada em `violations` e a fixture falha o teste
no fim, mesmo que o código sob teste engula a exceção (ex.: como `connection_error`).
"""

import asyncio
import ipaddress
import socket

ALLOWED_NAMES = {"localhost"}


class ExternalNetworkBlocked(Exception):
    """Não herda de OSError de propósito: nenhum `except OSError` do código sob teste a engole."""


def is_local(host) -> bool:
    if isinstance(host, bytes):
        host = host.decode("ascii", "ignore")
    if host in (None, ""):
        return True  # bind/any: sem destino externo
    host = str(host).strip("[]").lower()
    if host in ALLOWED_NAMES:
        return True
    try:
        return ipaddress.ip_address(host.split("%")[0]).is_loopback
    except ValueError:
        return False


def _host_of(address):
    return address[0] if isinstance(address, tuple) else address


class NetworkGuard:
    def __init__(self) -> None:
        self.violations: list[str] = []
        self._undo: list = []

    def _check(self, host, via: str) -> None:
        if not is_local(host):
            self.violations.append(f"{via} -> {host!r}")
            raise ExternalNetworkBlocked(f"conexão externa bloqueada nos testes: {via} -> {host!r}")

    def _patch(self, obj, name, make_wrapper) -> None:
        original = getattr(obj, name)
        setattr(obj, name, make_wrapper(original))
        self._undo.append((obj, name, original))

    def install(self) -> None:
        guard = self

        def sock_method(via):
            def make(original):
                def wrapper(sock, address, *a, **kw):
                    guard._check(_host_of(address), via)
                    return original(sock, address, *a, **kw)

                return wrapper

            return make

        self._patch(socket.socket, "connect", sock_method("socket.connect"))
        self._patch(socket.socket, "connect_ex", sock_method("socket.connect_ex"))

        def make_getaddrinfo(original):
            def wrapper(host, *a, **kw):
                guard._check(host, "getaddrinfo")
                return original(host, *a, **kw)

            return wrapper

        self._patch(socket, "getaddrinfo", make_getaddrinfo)

        def make_create_connection(original):
            async def wrapper(loop, protocol_factory, host=None, port=None, *a, **kw):
                guard._check(host, "loop.create_connection")
                return await original(loop, protocol_factory, host, port, *a, **kw)

            return wrapper

        self._patch(asyncio.BaseEventLoop, "create_connection", make_create_connection)

        def make_sock_connect(original):
            async def wrapper(loop, sock, address):
                guard._check(_host_of(address), "loop.sock_connect")
                return await original(loop, sock, address)

            return wrapper

        for loop_cls in _loop_classes():
            if "sock_connect" in vars(loop_cls):
                self._patch(loop_cls, "sock_connect", make_sock_connect)

    def uninstall(self) -> None:
        while self._undo:
            obj, name, original = self._undo.pop()
            setattr(obj, name, original)


def _loop_classes():
    from asyncio import proactor_events, selector_events

    candidates = [
        asyncio.BaseEventLoop,
        getattr(proactor_events, "BaseProactorEventLoop", None),
        getattr(selector_events, "BaseSelectorEventLoop", None),
    ]
    return [c for c in dict.fromkeys(candidates) if c is not None]
