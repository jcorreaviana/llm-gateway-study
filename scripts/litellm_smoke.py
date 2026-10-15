"""Smoke do LiteLLM Proxy (Marco 13): mock + proxy isolados, em 127.0.0.1 e portas dinâmicas.

O proxy roda no `.venv-litellm/` (nunca no `.venv` do projeto), com ambiente MÍNIMO (nenhuma variável do
usuário é herdada), master key aleatória gerada aqui, modo só-config (sem banco, sem UI) e três camadas
contra saída de rede:
  1. audit hook (`litellm/netguard/sitecustomize.py`) via PYTHONPATH: bloqueia e registra socket/DNS externos;
  2. proxy sumidouro (HTTP(S)_PROXY para uma porta local SEM serviço; conexão a ela vira violação);
  3. monitor externo: lista as conexões TCP do PID do proxy e dos filhos (PowerShell, só Windows).
Qualquer violação para a execução e mostra o destino. NENHUMA chamada à Anthropic (chave vazia em todos os
processos; o config nem tem provedor Anthropic).
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import secrets
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

import httpx
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _stack import (  # noqa: E402
    MOCK_HEALTH_TIMEOUT_S,
    REPO_ROOT,
    StackError,
    child_env,
    free_port,
    stop,
    wait_health,
)
from _tiktoken_cache import CL100K_URL, TiktokenCacheError, cache_key, expected_hashes, prepare_cache  # noqa: E402

VENV_PYTHON = REPO_ROOT / ".venv-litellm" / "Scripts" / "python.exe"
CONFIG = REPO_ROOT / "litellm" / "config.litellm.yaml"
NETGUARD_DIR = REPO_ROOT / "litellm" / "netguard"
OLLAMA = "http://127.0.0.1:11434"
LOOPBACK = {"127.0.0.1", "::1", "0.0.0.0", "::", "", "*"}
PROXY_HEALTH_TIMEOUT_S = 180
RATE_LIMIT_CHAOS = {"mode": "rate_limit", "retry_after_seconds": 5, "error_status": 500, "latency_ms": 0,
                    "fail_every_n": None, "fail_status": 500}
NORMAL_CHAOS = {"mode": "normal", "retry_after_seconds": 1, "error_status": 500, "latency_ms": 0,
                "fail_every_n": None, "fail_status": 500}
PROMPT = {"messages": [{"role": "user", "content": "Say hello in one word."}], "max_tokens": 8, "temperature": 0}
HEADER_HINTS = ("litellm", "fallback", "retry", "ratelimit", "provider")


class NetworkViolation(Exception):
    """O proxy tentou sair da máquina (hook, sumidouro ou monitor)."""


def proxy_available() -> str | None:
    """Motivo pelo qual o smoke não pode rodar, ou None."""
    if not VENV_PYTHON.exists():
        return ".venv-litellm/ nao existe (veja requirements-litellm.lock.txt)"
    probe = subprocess.run([str(VENV_PYTHON), "-c", "import litellm"], capture_output=True, timeout=120,
                           cwd=tempfile.gettempdir())
    return None if probe.returncode == 0 else "o pacote litellm nao esta instalado no .venv-litellm"


def derive_config(mock_port: int, dest: Path) -> Path:
    cfg = yaml.safe_load(CONFIG.read_text(encoding="utf-8"))
    for deployment in cfg["model_list"]:
        if deployment["model_name"] == "mock_market":
            deployment["litellm_params"]["api_base"] = f"http://127.0.0.1:{mock_port}/v1"
    dest.write_text(yaml.safe_dump(cfg, sort_keys=False), encoding="utf-8")
    return dest


def proxy_env(workdir: Path, master_key: str, sink_port: int, netguard_log: Path) -> dict[str, str]:
    """Ambiente mínimo do proxy: nada do ambiente do usuário é herdado."""
    system_root = os.environ.get("SYSTEMROOT", r"C:\Windows")
    tiktoken_dir = workdir / "tiktoken-cache"
    bundled = REPO_ROOT / ".venv-litellm" / "Lib" / "site-packages" / "litellm" / "litellm_core_utils" / "tokenizers"
    site_packages = REPO_ROOT / ".venv-litellm" / "Lib" / "site-packages"
    expected = expected_hashes(site_packages / "tiktoken_ext" / "openai_public.py")
    prepare_cache(bundled, tiktoken_dir, expected, required={cache_key(CL100K_URL)})
    sink = f"http://127.0.0.1:{sink_port}"
    return {
        "SYSTEMROOT": system_root, "PATH": rf"{system_root}\System32", "TEMP": str(workdir), "TMP": str(workdir),
        "PYTHONUNBUFFERED": "1", "PYTHONIOENCODING": "utf-8",
        # sem API real e sem chave
        "ENABLE_REAL_API": "0", "ANTHROPIC_API_KEY": "",
        "LITELLM_MASTER_KEY": master_key,
        # modo local: nada de baixar mapa de preços, posts, presets ou templates
        "LITELLM_MODE": "PRODUCTION", "LITELLM_LOCAL_MODEL_COST_MAP": "True", "LITELLM_LOCAL_BLOG_POSTS": "True",
        "LITELLM_LOCAL_ANTHROPIC_BETA_HEADERS": "True", "LITELLM_LOCAL_AUTOROUTER_PRESETS": "True",
        "LITELLM_LOCAL_POLICY_TEMPLATES": "True", "LITELLM_DONT_SHOW_FEEDBACK_BOX": "True",
        # telemetria: LITELLM_TELEMETRY NAO existe nesta versao; o desligamento e a flag --telemetry False
        "DO_NOT_TRACK": "1", "HF_HUB_OFFLINE": "1", "TRANSFORMERS_OFFLINE": "1",
        "CUSTOM_TIKTOKEN_CACHE_DIR": str(tiktoken_dir), "TIKTOKEN_CACHE_DIR": str(tiktoken_dir),
        # sem UI e sem docs
        "DISABLE_ADMIN_UI": "True", "NO_DOCS": "True", "NO_REDOC": "True",
        # proxy sumidouro: porta local sem serviço
        "HTTP_PROXY": sink, "HTTPS_PROXY": sink, "http_proxy": sink, "https_proxy": sink,
        "NO_PROXY": "127.0.0.1,localhost", "no_proxy": "127.0.0.1,localhost",
        # trava de rede (audit hook)
        "PYTHONPATH": str(NETGUARD_DIR), "NETGUARD_LOG": str(netguard_log), "NETGUARD_SINK_PORT": str(sink_port),
    }


def netguard_events(log: Path) -> list[dict]:
    if not log.exists():
        return []
    return [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines() if line.strip()]


def netguard_blocked(log: Path) -> list[dict]:
    return [e for e in netguard_events(log) if e["decision"].startswith("BLOCKED")]


def guard_self_test(workdir: Path, env: dict[str, str]) -> None:
    """Prova que a trava está armada NO python do .venv-litellm antes de subir o proxy."""
    log = workdir / "selftest.jsonl"
    env = {**env, "NETGUARD_LOG": str(log)}
    code = ("import socket\nfrom sitecustomize import NetguardViolation\n"
            "try:\n    socket.create_connection(('192.0.2.1', 9), timeout=1)\n"
            "except NetguardViolation:\n    print('BLOQUEADO')\n")
    out = subprocess.run([str(VENV_PYTHON), "-c", code], env=env, capture_output=True, text=True, timeout=60,
                         cwd=workdir)
    if "BLOQUEADO" not in out.stdout or not netguard_blocked(log):
        raise StackError(f"a trava de rede NAO esta armada no .venv-litellm:\n{out.stdout}\n{out.stderr}")


# ---------------------------------------------------------------- monitor externo (PowerShell)
PS_SCRIPT = r"""
$procs = Get-CimInstance Win32_Process | ForEach-Object { "$($_.ProcessId),$($_.ParentProcessId)" }
$conns = Get-NetTCPConnection -ErrorAction SilentlyContinue | ForEach-Object {
  "$($_.OwningProcess),$($_.LocalAddress),$($_.LocalPort),$($_.RemoteAddress),$($_.RemotePort),$($_.State)" }
"PROCS"; $procs; "CONNS"; $conns
"""


def process_tree(root: int, pairs: list[tuple[int, int]]) -> set[int]:
    tree, frontier = {root}, [root]
    while frontier:
        parent = frontier.pop()
        for pid, ppid in pairs:
            if ppid == parent and pid not in tree:
                tree.add(pid)
                frontier.append(pid)
    return tree


def sample_connections(root_pid: int) -> list[dict]:
    """Conexões TCP do PID do proxy e dos filhos, num instante."""
    if sys.platform != "win32":
        return []
    raw = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", PS_SCRIPT],
                         capture_output=True, text=True, timeout=60).stdout.splitlines()
    section, pairs, conns = "", [], []
    for line in (item.strip() for item in raw):
        if line in ("PROCS", "CONNS"):
            section = line
        elif section == "PROCS" and line.count(",") == 1:
            a, b = line.split(",")
            if a.isdigit() and b.isdigit():
                pairs.append((int(a), int(b)))
        elif section == "CONNS" and line.count(",") == 5:
            pid, laddr, lport, raddr, rport, state = line.split(",")
            conns.append({"pid": int(pid), "local": f"{laddr}:{lport}", "remote_addr": raddr,
                          "remote": f"{raddr}:{rport}", "state": state})
    tree = process_tree(root_pid, pairs)
    return [dict(c, tree=sorted(tree)) for c in conns if c["pid"] in tree]


@dataclass
class ConnectionMonitor:
    root_pid: int
    seen: dict[tuple[str, str], dict] = field(default_factory=dict)
    samples: int = 0
    pids: set[int] = field(default_factory=set)
    _stop: threading.Event = field(default_factory=threading.Event)
    _thread: threading.Thread | None = None

    def sample_once(self) -> None:
        for c in sample_connections(self.root_pid):
            self.pids.update(c["tree"])
            self.seen.setdefault((c["remote"], c["state"]), c)
        self.samples += 1

    def start(self) -> None:
        def loop() -> None:
            while not self._stop.is_set():
                self.sample_once()
        self._thread = threading.Thread(target=loop, daemon=True)
        self._thread.start()

    def finish(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=60)
        self.sample_once()

    def external(self) -> list[dict]:
        return [c for c in self.seen.values() if c["remote_addr"] not in LOOPBACK]


# ---------------------------------------------------------------- execução
@dataclass
class SmokeReport:
    steps: list[dict] = field(default_factory=list)
    mock_stats: list[dict] = field(default_factory=list)
    connections: list[dict] = field(default_factory=list)
    external_connections: list[dict] = field(default_factory=list)
    netguard_events: list[dict] = field(default_factory=list)
    netguard_blocked: list[dict] = field(default_factory=list)
    monitor_samples: int = 0
    proxy_pids: list[int] = field(default_factory=list)
    orphans: list[int] = field(default_factory=list)


def pid_alive(pid: int) -> bool:
    out = subprocess.run(["tasklist", "/FI", f"PID eq {pid}", "/NH"], capture_output=True, text=True).stdout
    return str(pid) in out


def kill_tree(proc: subprocess.Popen) -> None:
    if proc.poll() is None:
        subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"], capture_output=True)
        with contextlib.suppress(subprocess.TimeoutExpired):
            proc.wait(timeout=10)


def interesting_headers(resp: httpx.Response) -> dict[str, str]:
    return {k: v for k, v in resp.headers.items() if any(h in k.lower() for h in HEADER_HINTS)}


def chat(client: httpx.Client, base: str, key: str, model: str) -> dict:
    t0 = time.perf_counter()
    resp = client.post(f"{base}/v1/chat/completions", json={**PROMPT, "model": model},
                       headers={"Authorization": f"Bearer {key}"}, timeout=180)
    body = {}
    with contextlib.suppress(ValueError):
        body = resp.json()
    return {"requested": model, "status": resp.status_code, "latency_ms": round((time.perf_counter() - t0) * 1000),
            "model_in_body": body.get("model"), "headers": interesting_headers(resp),
            "reply": (body.get("choices") or [{}])[0].get("message", {}).get("content", "")[:40]}


def ollama_ready() -> str | None:
    try:
        tags = httpx.get(f"{OLLAMA}/api/tags", timeout=3).json()
    except (httpx.HTTPError, ValueError):
        return "Ollama nao responde em 127.0.0.1:11434"
    names = {m.get("name") for m in tags.get("models", [])}
    missing = [m for m in ("qwen2.5:3b", "llama3.2:3b") if m not in names]
    return f"faltam modelos no Ollama: {missing}" if missing else None


def check_violations(log: Path, monitor: ConnectionMonitor | None) -> None:
    blocked = netguard_blocked(log)
    if blocked:
        raise NetworkViolation("trava de rede (hook/sumidouro) bloqueou: "
                               + "; ".join(f"{b['event']} -> {b['target']} [{b['decision']}]" for b in blocked))
    if monitor and monitor.external():
        raise NetworkViolation("monitor externo viu conexao fora do loopback: "
                               + "; ".join(f"pid {c['pid']} {c['remote']} {c['state']}" for c in monitor.external()))


def run_smoke(*, with_fallback: bool = True, keep_workdir: bool = False) -> SmokeReport:
    reason = proxy_available()
    if reason:
        raise StackError(reason)
    if with_fallback and (reason := ollama_ready()):
        raise StackError(reason)
    report = SmokeReport()
    workdir = Path(tempfile.mkdtemp(prefix="litellm-smoke-"))
    mock_port, proxy_port, sink_port = free_port(), free_port(), free_port()  # sink_port: ninguém escuta
    while len({mock_port, proxy_port, sink_port}) < 3:
        proxy_port, sink_port = free_port(), free_port()
    master_key = "sk-" + secrets.token_hex(16)  # aleatória, só na memória e no ambiente do filho
    netguard_log = workdir / "netguard.jsonl"
    config = derive_config(mock_port, workdir / "config.litellm.yaml")
    try:
        env = proxy_env(workdir, master_key, sink_port, netguard_log)
    except TiktokenCacheError as exc:
        shutil.rmtree(workdir, ignore_errors=True)
        raise StackError(f"cache do tiktoken nao verificavel (abortando; quem decide e o usuario): {exc}") from exc
    base = f"http://127.0.0.1:{proxy_port}"
    mock_url = f"http://127.0.0.1:{mock_port}"
    procs: list[subprocess.Popen] = []
    handles = [open(workdir / f"{n}.log", "wb") for n in ("mock", "proxy")]
    monitor: ConnectionMonitor | None = None
    try:
        guard_self_test(workdir, env)
        flags = subprocess.CREATE_NEW_PROCESS_GROUP if sys.platform == "win32" else 0
        mock = subprocess.Popen([sys.executable, "-m", "mock_upstream.server"], cwd=REPO_ROOT,
                                env=child_env(MOCK_UPSTREAM_PORT=str(mock_port)), stdout=handles[0],
                                stderr=subprocess.STDOUT, creationflags=flags)
        procs.append(mock)
        wait_health(mock_url, mock, workdir / "mock.log", MOCK_HEALTH_TIMEOUT_S)
        proxy = subprocess.Popen(
            [str(VENV_PYTHON), "-m", "litellm.proxy.proxy_cli", "--config", str(config), "--host", "127.0.0.1",
             "--port", str(proxy_port), "--num_workers", "1", "--telemetry", "False"],
            cwd=workdir, env=env, stdout=handles[1], stderr=subprocess.STDOUT, creationflags=flags)
        procs.append(proxy)
        monitor = ConnectionMonitor(proxy.pid)
        monitor.start()
        deadline = time.monotonic() + PROXY_HEALTH_TIMEOUT_S
        while True:
            check_violations(netguard_log, monitor)
            if proxy.poll() is not None:
                tail = (workdir / "proxy.log").read_text(encoding="utf-8", errors="replace")[-1500:]
                raise StackError(f"o proxy encerrou antes de ficar pronto (codigo {proxy.returncode}):\n{tail}")
            try:
                if httpx.get(f"{base}/health/liveliness", timeout=1).status_code == 200:
                    break
            except httpx.HTTPError:
                time.sleep(0.3)
            if time.monotonic() > deadline:
                raise StackError("o proxy nao ficou pronto a tempo")
        with httpx.Client() as client:
            def mock_stats() -> dict:
                return client.get(f"{mock_url}/stats", timeout=5).json()

            def step(label: str, model: str) -> None:
                result = chat(client, base, master_key, model)
                result["label"] = label
                report.steps.append(result)
                report.mock_stats.append({"after": label, **mock_stats()})
                check_violations(netguard_log, monitor)

            client.post(f"{mock_url}/stats/reset", timeout=5)
            client.post(f"{mock_url}/chaos", json=NORMAL_CHAOS, timeout=5).raise_for_status()
            step("a) mock normal", "mock_market")
            if with_fallback:
                client.post(f"{mock_url}/chaos", json=RATE_LIMIT_CHAOS, timeout=5).raise_for_status()
                step("b) mock rate_limit", "mock_market")
        monitor.finish()
        check_violations(netguard_log, monitor)
        report.proxy_pids = sorted(monitor.pids)
        report.connections = sorted(monitor.seen.values(), key=lambda c: (c["state"], c["remote"]))
        report.monitor_samples = monitor.samples
    finally:
        if monitor:
            monitor._stop.set()
        for proc in reversed(procs):
            kill_tree(proc) if proc.args[0] != sys.executable else stop(proc)
        for h in handles:
            h.close()
        report.netguard_events = netguard_events(netguard_log)
        report.netguard_blocked = netguard_blocked(netguard_log)
        if monitor:
            report.external_connections = monitor.external()
            report.proxy_pids = sorted(monitor.pids | set(report.proxy_pids))
        time.sleep(1)
        report.orphans = [p.pid for p in procs if pid_alive(p.pid)] + [p for p in report.proxy_pids if pid_alive(p)]
        if not keep_workdir:
            shutil.rmtree(workdir, ignore_errors=True)
    return report


def print_report(report: SmokeReport) -> None:
    print("\n=== Requisicoes ===")
    for s in report.steps:
        print(f"{s['label']}: status {s['status']} em {s['latency_ms']} ms; "
              f"model no corpo={s['model_in_body']!r}; texto={s['reply']!r}")
        for k, v in sorted(s["headers"].items()):
            print(f"    {k}: {v}")
    print("\n=== /stats do mock ===")
    for m in report.mock_stats:
        print(f"apos {m['after']}: {json.dumps({k: v for k, v in m.items() if k != 'after'})}")
    print("\n=== Conexoes TCP do processo do proxy e filhos (monitor externo) ===")
    print(f"PIDs observados: {report.proxy_pids}; amostras: {report.monitor_samples}")
    remotes = {}
    for c in report.connections:
        remotes.setdefault((c["remote"], c["state"]), 0)
        remotes[(c["remote"], c["state"])] += 1
    for (remote, state), _ in sorted(remotes.items()):
        print(f"    remoto {remote:<24} estado {state}")
    print(f"conexoes fora do loopback: {len(report.external_connections)}")
    armed = [e for e in report.netguard_events if e["decision"] == "armed"]
    allowed = [e for e in report.netguard_events if e["decision"] == "allowed"]
    print("\n=== Trava de rede (audit hook) ===")
    print(f"processos Python com a trava armada: {len({e['pid'] for e in armed})}; eventos permitidos (loopback): "
          f"{len(allowed)}; BLOQUEADOS: {len(report.netguard_blocked)}")
    for e in report.netguard_blocked:
        print(f"    BLOQUEADO {e['event']} -> {e['target']} ({e['decision']})")
    print(f"\nprocessos restantes apos encerrar: {report.orphans or 'nenhum'}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--no-fallback", action="store_true", help="so o cenario (a); nao precisa do Ollama")
    parser.add_argument("--keep-workdir", action="store_true")
    args = parser.parse_args()
    try:
        report = run_smoke(with_fallback=not args.no_fallback, keep_workdir=args.keep_workdir)
    except NetworkViolation as exc:
        print(f"\nPARADO: {exc}", file=sys.stderr)
        return 3
    except StackError as exc:
        print(f"erro: {exc}", file=sys.stderr)
        return 2
    print_report(report)
    return 1 if report.orphans or report.netguard_blocked or report.external_connections else 0


if __name__ == "__main__":
    sys.exit(main())
