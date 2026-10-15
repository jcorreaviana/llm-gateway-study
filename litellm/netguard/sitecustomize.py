"""Trava de saída de rede para o processo do LiteLLM Proxy (só para o smoke do Marco 13).

Como funciona: o Python importa `sitecustomize` na inicialização de QUALQUER interpretador cujo
`PYTHONPATH` contenha este diretório (o proxy e os filhos que ele criar herdam a variável). Aqui
instalamos um audit hook (`sys.addaudithook`) que, para cada evento de socket ou de resolução de nomes
cujo destino NÃO seja loopback, registra a tentativa e levanta `NetguardViolation` ANTES de a chamada
de sistema acontecer (nenhum pacote sai, nenhuma consulta DNS é feita).

Variáveis de ambiente:
  NETGUARD_LOG        arquivo JSONL (em diretório temporário) onde cada decisão é registrada.
  NETGUARD_SINK_PORT  porta do "proxy sumidouro" (HTTP_PROXY/HTTPS_PROXY). Uma conexão para ela significa
                      que alguma biblioteca tentou SAIR pela internet via proxy; também é bloqueada e
                      registrada como violação (a porta não tem serviço, então nada responderia).

Loopback = `localhost` ou IP literal que `ipaddress` classifica como loopback (127.0.0.0/8 e ::1).

Limitações (documentadas de propósito):
  * O hook só vê sockets abertos pelo Python. Extensões nativas que abrem sockets por conta própria
    (hf-xet, granian, tiktoken, tokenizers, etc.) NÃO passam por ele. A segunda camada é o monitor
    externo, que lista as conexões do PID do proxy e dos filhos.
  * No Windows, o asyncio (Proactor) conecta a um IP literal com `ConnectEx`, que não gera evento de
    auditoria. Por isso `create_connection` e `sock_connect` do asyncio também são verificados aqui.
  * `NetguardViolation` NÃO herda de `OSError`: o código sob teste não deve engoli-la como uma falha
    comum de conexão. Quem lê o veredito é o arquivo de log, não a exceção.
"""

from __future__ import annotations

import ipaddress
import json
import os
import sys
import threading
import time

_LOG = os.environ.get("NETGUARD_LOG", "")
try:
    _SINK_PORT = int(os.environ.get("NETGUARD_SINK_PORT", "") or 0)
except ValueError:
    _SINK_PORT = 0

# Eventos de auditoria de socket e onde está o destino em `args`.
_ADDRESS_EVENTS = {"socket.connect": 1, "socket.sendto": 1, "socket.getnameinfo": 0}
_HOST_EVENTS = {"socket.getaddrinfo", "socket.gethostbyname", "socket.gethostbyname_ex",
                "socket.gethostbyaddr"}

_reentrant = threading.local()


class NetguardViolation(Exception):
    """Tentativa de saída de rede bloqueada pela trava."""


def _is_loopback(host: object, *, allow_empty: bool = False) -> bool:
    if host is None or host == "" or host == b"":
        return allow_empty  # getaddrinfo(None) é a resolução do "curinga" (servidor), sem destino externo
    if isinstance(host, bytes):
        host = host.decode("ascii", "replace")
    if not isinstance(host, str):
        return False
    name = host.split("%", 1)[0].strip().lower().rstrip(".")
    if name == "localhost":
        return True
    try:
        ip = ipaddress.ip_address(name)
    except ValueError:
        return False  # nome de host que não seja localhost: externo
    return ip.is_loopback or (ip.version == 6 and ip.ipv4_mapped is not None and ip.ipv4_mapped.is_loopback)


def _record(event: str, target: str, decision: str) -> None:
    if not _LOG:
        return
    line = json.dumps({"t": round(time.time(), 3), "pid": os.getpid(), "event": event,
                       "target": target, "decision": decision}) + "\n"
    try:
        fd = os.open(_LOG, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
        try:
            os.write(fd, line.encode("utf-8"))
        finally:
            os.close(fd)
    except OSError:
        pass  # sem log a trava continua valendo (a exceção sai de qualquer jeito)


def _check(event: str, host: object, port: object = None, *, allow_empty: bool = False) -> None:
    target = f"{host!r}:{port}" if port is not None else repr(host)
    if not _is_loopback(host, allow_empty=allow_empty):
        _record(event, target, "BLOCKED")
        raise NetguardViolation(f"saida de rede bloqueada ({event}): {target}")
    if _SINK_PORT and port == _SINK_PORT:
        _record(event, target, "BLOCKED_SINK")
        raise NetguardViolation(f"tentativa de sair pelo proxy sumidouro ({event}): {target}")
    _record(event, target, "allowed")


def _split_address(address: object) -> tuple[object, object] | None:
    """(host, porta) de um endereço de socket; None para AF_UNIX (caminho) ou formato desconhecido."""
    if isinstance(address, tuple) and len(address) >= 2:
        return address[0], address[1]
    return None


def _audit(event: str, args: tuple) -> None:
    if not event.startswith("socket."):
        return
    if getattr(_reentrant, "busy", False):
        return
    _reentrant.busy = True
    try:
        if event in _ADDRESS_EVENTS:
            split = _split_address(args[_ADDRESS_EVENTS[event]])
            if split is not None:
                _check(event, split[0], split[1])
        elif event in _HOST_EVENTS:
            _check(event, args[0], allow_empty=(event == "socket.getaddrinfo"))
    finally:
        _reentrant.busy = False


def _patch_asyncio() -> None:
    """Verifica também o destino em asyncio: no Windows (Proactor) o IP literal não gera evento."""
    import asyncio.base_events as base_events

    original_create = base_events.BaseEventLoop.create_connection

    async def create_connection(self, protocol_factory, host=None, port=None, *args, **kwargs):
        if kwargs.get("sock") is None and host is not None:
            _check("asyncio.create_connection", host, port)
        return await original_create(self, protocol_factory, host, port, *args, **kwargs)

    base_events.BaseEventLoop.create_connection = create_connection

    modules = [("asyncio.selector_events", "BaseSelectorEventLoop"),
               ("asyncio.proactor_events", "BaseProactorEventLoop")]
    for module_name, class_name in modules:
        try:
            module = __import__(module_name, fromlist=[class_name])
            cls = getattr(module, class_name)
        except (ImportError, AttributeError):
            continue
        original_sock_connect = cls.sock_connect

        def make(original):
            async def sock_connect(self, sock, address):
                split = _split_address(address)
                if split is not None:
                    _check("asyncio.sock_connect", split[0], split[1])
                return await original(self, sock, address)
            return sock_connect

        cls.sock_connect = make(original_sock_connect)


def _arm() -> None:
    sys.addaudithook(_audit)
    _patch_asyncio()
    _record("netguard.armed", f"argv0={os.path.basename(sys.argv[0]) if sys.argv else ''}", "armed")


_arm()
