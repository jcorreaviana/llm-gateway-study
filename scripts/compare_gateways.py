"""Runner da comparação de comportamento (Marco 14, Fase 2): o gateway do projeto x o LiteLLM Proxy.

Mesmas cenas, mesmo mock, mesmo Ollama; UM gateway por vez (o próprio, depois o LiteLLM) e o estado é zerado
a cada repetição (o gateway e o mock são reiniciados por cena e por repetição). O proxy do LiteLLM sobe SEMPRE
com a trava de rede, o proxy sumidouro e o monitor de conexões do Marco 13 (`litellm_smoke`); qualquer
violação PARA a execução e mostra o destino. NENHUMA chamada à Anthropic (chave vazia, nenhum provedor
Anthropic habilitado). Não é benchmark: compara comportamento num cenário.

    .venv\\Scripts\\python scripts\\compare_gateways.py --run-id m14-full
    .venv\\Scripts\\python scripts\\compare_gateways.py --run-id probes --probes

Os resultados brutos ficam em `compare_results/<run-id>/` (ignorado pelo git). Só agregados vão para o
repositório (`python -m comparekit.report --run-id <id>`).
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import platform
import secrets
import shutil
import subprocess
import sys
import tempfile
import time
from collections import Counter
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx
import yaml

SCRIPTS_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS_DIR))
sys.path.insert(0, str(SCRIPTS_DIR.parent))
import litellm_smoke as smoke  # noqa: E402
from _stack import (  # noqa: E402
    MOCK_HEALTH_TIMEOUT_S,
    REPO_ROOT,
    StackError,
    child_env,
    free_port,
    free_ports,
    start_stack,
    stop,
    wait_health,
)
from _tiktoken_cache import TiktokenCacheError  # noqa: E402
from demo import build_runtime_config  # noqa: E402

from comparekit import metrics  # noqa: E402

RESULTS_ROOT = REPO_ROOT / "compare_results"
DEMO_CONFIG = REPO_ROOT / "config.demo.yaml"
OLLAMA_PORT = 11434
GATEWAYS = ("proprio", "litellm", "litellm_af2")
# Variante do LiteLLM com `allowed_fails: 2`: o valor 3 do config do Marco 13 é IGUAL ao padrão da biblioteca e o
# código instalado o trata como "não definido" (`_is_allowed_fails_set_on_router`); com `>` no limite, 2 é o valor
# que corresponde a "cooldown na 3ª falha". Só roda nas cenas sensíveis a isso.
VARIANT_ROUTER = {"litellm_af2": {"allowed_fails": 2}}
VARIANT_SCENES = {"litellm_af2": frozenset({"3c", "3d", "4", "5a", "5b", "6"})}
REPS = 3
MAX_TOKENS = 16
PROMPTS = ("Say hello in one word.", "Name one color.", "Count from one to three.", "Reply with the number four.",
           "Name one planet.", "Say yes or no.")
FILLER = "The quick brown fox jumps over the lazy dog. "  # texto genérico repetido (nenhum dado real)
SMALL_PROMPT_CHARS = 16_000    # ~4000 tokens pela estimativa chars/4: acima do Ollama (3072), abaixo do mock (8192)
LARGE_PROMPT_CHARS = 44_000    # ~11000 tokens pela estimativa chars/4: acima de todos os provedores
SLOW_MS = 2000                 # latência do mock na cena de recuperação (para ver a sonda half-open)
PROBE_WINDOW_S = SLOW_MS / 1000
TIMEOUT_MARGIN_S = 30
DEAD_VARIANT = "dead_ollama"
RPM_FLAG = "rpm"   # só a cena 2 roda com o rpm do mock_market; nas demais o rpm é removido nos DOIS gateways


def classify_target(target: str, ports: dict[str, int]) -> str:
    """Rótulo estável para o log da trava: portas dinâmicas viram o papel do processo."""
    host, _, port = target.rpartition(":")
    if port.isdigit():
        role = {ports["mock"]: "mock", ports["proxy"]: "proxy", ports["sink"]: "SUMIDOURO",
                OLLAMA_PORT: "ollama", ports.get("dead", -1): "porta-sem-servico"}.get(int(port))
        if role:
            return f"{host}:{role}"
    return target


# --------------------------------------------------------------------------- alvos
@dataclass
class Target:
    kind: str                       # "proprio" | "litellm" (variantes do LiteLLM têm kind "litellm")
    base: str
    mock_url: str
    auth: dict[str, str] = field(default_factory=dict)
    gateway_id: str = ""

    @property
    def default_model(self) -> str | None:
        return "mock_market" if self.kind == "litellm" else None

    def parse(self, resp: httpx.Response) -> tuple[str, int | None, str]:
        """(provedor que respondeu, tentativas de fallback, motivo do fallback quando o gateway informa)."""
        h = resp.headers
        if self.kind == "proprio":
            reason = h.get("x-gateway-fallback-reason", "")
            return h.get("x-gateway-provider", "none"), len([r for r in reason.split(",") if r]), reason
        attempted = h.get("x-litellm-attempted-fallbacks")
        return h.get("x-litellm-model-id", "none"), int(attempted) if attempted and attempted.isdigit() else None, ""


def relevant_headers(resp: httpx.Response) -> dict[str, str]:
    keep = ("x-gateway-", "x-litellm-model-id", "x-litellm-model-group", "x-litellm-attempted", "retry-after",
            "x-ratelimit", "x-litellm-version")
    return {k: v for k, v in sorted(resp.headers.items()) if k.lower().startswith(keep)}


class Scene:
    """Estado de UMA execução de cena: requisições registradas e contador de chamadas ao mock."""

    def __init__(self, target: Target, client: httpx.AsyncClient) -> None:
        self.target, self.client = target, client
        self.requests: list[dict[str, Any]] = []
        self.extra: dict[str, Any] = {}
        self.time_to_mock_s: float | None = None
        self.t0 = time.perf_counter()
        self._prev_received = 0

    def now(self) -> float:
        return time.perf_counter() - self.t0

    # -- mock
    async def chaos(self, **kw: Any) -> None:
        body = {"mode": "normal", "retry_after_seconds": 1, "error_status": 500, "latency_ms": 0,
                "fail_every_n": None, "fail_status": 500, **kw}
        (await self.client.post(f"{self.target.mock_url}/chaos", json=body, timeout=10)).raise_for_status()

    async def mock_stats(self) -> dict[str, Any]:
        return (await self.client.get(f"{self.target.mock_url}/stats", timeout=10)).json()

    async def reset_mock(self) -> None:
        await self.client.post(f"{self.target.mock_url}/stats/reset", timeout=10)
        await self.chaos()
        self._prev_received = 0

    # -- requisição
    async def ask(self, *, label: str = "", prompt: str | None = None, model: str | None = None,
                  headers: dict[str, str] | None = None, body_extra: dict[str, Any] | None = None,
                  track: bool = True) -> dict[str, Any]:
        i = len(self.requests) + 1
        text = prompt if prompt is not None else PROMPTS[(i - 1) % len(PROMPTS)]
        body: dict[str, Any] = {"messages": [{"role": "user", "content": text}], "max_tokens": MAX_TOKENS,
                                "temperature": 0, **(body_extra or {})}
        model = model or self.target.default_model
        if model:
            body["model"] = model
        rec: dict[str, Any] = {"i": i, "label": label, "t_s": round(self.now(), 3), "prompt_chars": len(text)}
        self.requests.append(rec)
        start = time.perf_counter()
        resp = await self.client.post(f"{self.target.base}/v1/chat/completions", json=body, timeout=180,
                                      headers={**self.target.auth, **(headers or {})})
        rec["latency_ms"] = round((time.perf_counter() - start) * 1000)
        provider, attempts, reason = self.target.parse(resp)
        rec.update(status=resp.status_code, provider=provider, attempts=attempts, fallback_reason=reason,
                   headers=relevant_headers(resp))
        data: Any = None
        with contextlib.suppress(ValueError):
            data = resp.json()
        if resp.status_code == 200 and isinstance(data, dict):
            content = ((data.get("choices") or [{}])[0].get("message") or {}).get("content") or ""
            rec["reply_chars"] = len(content)
        else:
            rec["error_body"] = resp.text[:4000]  # texto de ERRO do gateway/mock (nunca resposta de modelo)
            rec["error_keys"] = sorted(data.keys()) if isinstance(data, dict) else None
        if track:
            stats = await self.mock_stats()
            rec["mock_received"] = stats["received"]
            rec["reached_mock"] = stats["received"] > self._prev_received
            self._prev_received = stats["received"]
        return rec

    async def poll_until_mock(self, *, interval_s: float, cap_s: float, label: str) -> None:
        """Uma requisição a cada `interval_s` (envio a envio) até uma chegar ao mock, ou `cap_s`."""
        t0 = self.now()
        next_t = t0
        while self.now() - t0 < cap_s:
            await asyncio.sleep(max(0.0, next_t - self.now()))
            next_t = self.now() + interval_s
            if (await self.ask(label=label))["reached_mock"]:
                return


def synthetic_prompt(chars: int) -> str:
    return (FILLER * (chars // len(FILLER) + 1))[:chars]


# --------------------------------------------------------------------------- cenas
async def scene_normal(s: Scene) -> None:
    await s.reset_mock()
    for _ in range(3):
        await s.ask(label="normal")


async def scene_local_limit(s: Scene) -> None:
    await s.reset_mock()
    rpm = 10  # rate_limit.rpm do mock_market no config da demo e `rpm` do deployment no LiteLLM
    for _ in range(rpm + 3):
        await s.ask(label="acima do rpm")
    s.extra["rpm"] = rpm


def retry_after_scene(seconds: int) -> Callable[[Scene], Awaitable[None]]:
    async def run(s: Scene) -> None:
        await s.reset_mock()
        await s.chaos(mode="rate_limit", retry_after_seconds=seconds)
        await s.ask(label=f"429 Retry-After={seconds}")
        await s.chaos()                         # o mock volta ao normal; a espera medida é só a do gateway
        t0 = s.now()
        await s.poll_until_mock(interval_s=0.5, cap_s=seconds * 2 + TIMEOUT_MARGIN_S, label="sondagem")
        s.time_to_mock_s = metrics.time_to_return_s(s.requests, t0)
        s.extra["retry_after_s"] = seconds
    return run


def repeated_429_scene(seconds: int) -> Callable[[Scene], Awaitable[None]]:
    """429 repetidos até o gateway parar de chamar o mock (se parar); depois mede quando ele volta, com o
    `Retry-After` curto ou longo. Separa "respeita o valor do header" de "usa um cooldown fixo"."""
    async def run(s: Scene) -> None:
        await s.reset_mock()
        await s.chaos(mode="rate_limit", retry_after_seconds=seconds)
        last_reach_end = s.now()
        for _ in range(8):
            rec = await s.ask(label=f"429 repetido, Retry-After={seconds}")
            if rec["reached_mock"]:
                last_reach_end = s.now()
            if metrics.calls_until_stop([r["mock_received"] for r in s.requests], tail=2) is not None:
                break
        s.extra["tripped"] = metrics.calls_until_stop([r["mock_received"] for r in s.requests], tail=2) is not None
        s.extra["calls_until_stop"] = metrics.calls_until_stop([r["mock_received"] for r in s.requests], tail=2)
        await s.chaos()                      # o mock volta ao normal; mede-se só a espera do gateway
        await s.poll_until_mock(interval_s=0.5, cap_s=seconds * 2 + TIMEOUT_MARGIN_S, label="sondagem")
        s.time_to_mock_s = metrics.time_to_return_s(s.requests, last_reach_end)
        s.extra["retry_after_s"] = seconds
    return run


async def scene_continuous_failure(s: Scene) -> None:
    await s.reset_mock()
    await s.chaos(mode="server_error", error_status=500)
    for _ in range(10):
        await s.ask(label="500 continuo")
    s.extra["calls_until_stop"] = metrics.calls_until_stop([r["mock_received"] for r in s.requests])


def interleaved_scene(pattern: str) -> Callable[[Scene], Awaitable[None]]:
    async def run(s: Scene) -> None:
        await s.reset_mock()
        for ch in pattern:
            await (s.chaos(mode="server_error", error_status=500) if ch == "F" else s.chaos())
            await s.ask(label=f"{'falha' if ch == 'F' else 'sucesso'} (padrao {pattern})")
        s.extra.update(pattern=pattern, first_skipped=metrics.first_skipped_index(s.requests))
    return run


async def scene_recovery(s: Scene) -> None:
    await s.reset_mock()
    await s.chaos(mode="server_error", error_status=500)
    for _ in range(10):
        await s.ask(label="derrubando o mock")
        if metrics.calls_until_stop([r["mock_received"] for r in s.requests], tail=2) is not None:
            break
    s.extra["tripped"] = metrics.calls_until_stop([r["mock_received"] for r in s.requests], tail=2) is not None
    base = (await s.mock_stats())["received"]
    # O mock volta, lento de propósito: a primeira chamada que chegar a ele leva SLOW_MS, e as sondagens
    # seguintes (sem esperar a resposta) mostram se o gateway deixa passar uma sonda só ou todas.
    await s.chaos(mode="slow", latency_ms=SLOW_MS)
    t0 = s.now()
    tasks: list[asyncio.Task] = []
    detected_at: float | None = None
    while s.now() - t0 < 60:
        tasks.append(asyncio.create_task(s.ask(label="sondagem pos-recuperacao", track=False)))
        await asyncio.sleep(0.25)
        if detected_at is None and (await s.mock_stats())["received"] > base:
            detected_at = s.now()
        if detected_at is not None and s.now() - detected_at > PROBE_WINDOW_S + 2:
            break
    await asyncio.gather(*tasks)
    for rec in s.requests:
        if rec["label"] == "sondagem pos-recuperacao":
            rec["reached_mock"] = rec["provider"] == metrics.MOCK_PROVIDER
    s.requests.sort(key=lambda r: r["t_s"])
    polls = [r for r in s.requests if r["label"] == "sondagem pos-recuperacao"]
    s.time_to_mock_s = metrics.time_to_return_s(polls, t0)
    s.extra["probe_window"] = metrics.window_counts(polls, PROBE_WINDOW_S)
    s.extra["mock_calls_after_recovery"] = (await s.mock_stats())["received"] - base


async def scene_all_unavailable(s: Scene) -> None:
    await s.reset_mock()
    await s.chaos(mode="rate_limit", retry_after_seconds=5)
    await s.ask(label="tudo fora (1a)")
    await s.ask(label="tudo fora (2a, logo depois)")
    s.extra["ollama"] = "porta local sem servico (variante dead_ollama)"


async def scene_all_unavailable_500(s: Scene) -> None:
    """Igual à cena 7, mas o mock responde 500 (e não 429): o status do erro final depende de qual falha foi a primeira
    ou a última? Os Ollama seguem apontados para uma porta sem serviço (erro de conexão)."""
    await s.reset_mock()
    await s.chaos(mode="server_error", error_status=500)
    await s.ask(label="tudo fora, mock em 500 (1a)")
    await s.ask(label="tudo fora, mock em 500 (2a, logo depois)")
    s.extra["ollama"] = "porta local sem servico (variante dead_ollama)"


async def scene_context_large(s: Scene) -> None:
    await s.reset_mock()
    await s.ask(label="prompt acima de todas as janelas", prompt=synthetic_prompt(LARGE_PROMPT_CHARS))
    await s.ask(label="controle (prompt curto)")
    s.extra["prompt_chars"] = LARGE_PROMPT_CHARS


async def scene_context_medium(s: Scene) -> None:
    await s.reset_mock()
    await s.chaos(mode="rate_limit", retry_after_seconds=1)
    await s.ask(label="prompt acima do Ollama, abaixo do mock; mock em 429",
                prompt=synthetic_prompt(SMALL_PROMPT_CHARS))
    s.extra["prompt_chars"] = SMALL_PROMPT_CHARS


async def scene_force(s: Scene) -> None:
    await s.reset_mock()
    if s.target.kind == "proprio":
        force = {"x-gateway-force-provider": "ollama_qwen"}
        await s.ask(label="forcar ollama_qwen", headers=force)
        await s.chaos(mode="rate_limit", retry_after_seconds=2)
        await s.ask(label="forcar mock_market com o mock em 429", headers={"x-gateway-force-provider": "mock_market"})
        await s.ask(label="forcar provedor desconhecido", headers={"x-gateway-force-provider": "inexistente"})
        return
    await s.ask(label="model=ollama_qwen", model="ollama_qwen")
    await s.chaos(mode="rate_limit", retry_after_seconds=2)
    await s.ask(label="model=mock_market com o mock em 429", model="mock_market")
    await s.ask(label="model=mock_market + disable_fallbacks no corpo", model="mock_market",
                body_extra={"disable_fallbacks": True})
    await s.ask(label="model desconhecido", model="inexistente")


SCENES: list[tuple[str, str, Callable[[Scene], Awaitable[None]], frozenset[str]]] = [
    ("1", "Normal", scene_normal, frozenset()),
    ("2", "Limite local (rpm)", scene_local_limit, frozenset({RPM_FLAG})),
    ("3a", "429 com Retry-After curto (2 s)", retry_after_scene(2), frozenset()),
    ("3b", "429 com Retry-After longo (15 s)", retry_after_scene(15), frozenset()),
    ("3c", "429 repetidos com Retry-After curto (2 s)", repeated_429_scene(2), frozenset()),
    ("3d", "429 repetidos com Retry-After longo (15 s)", repeated_429_scene(15), frozenset()),
    ("4", "Falha 5xx continua", scene_continuous_failure, frozenset()),
    ("5a", "Falhas intercaladas (2 falhas, 1 sucesso, x4)", interleaved_scene("FFS" * 4), frozenset()),
    ("5b", "Falhas intercaladas (alternando falha e sucesso)", interleaved_scene("FS" * 5), frozenset()),
    ("6", "Recuperacao (mock volta lento)", scene_recovery, frozenset()),
    ("7", "Tudo indisponivel", scene_all_unavailable, frozenset({DEAD_VARIANT})),
    ("7b", "Tudo indisponivel, mock em 500", scene_all_unavailable_500, frozenset({DEAD_VARIANT})),
    ("8a", "Prompt maior que todas as janelas", scene_context_large, frozenset()),
    ("8b", "Prompt maior que a janela do Ollama", scene_context_medium, frozenset()),
    ("9", "Forcar provedor", scene_force, frozenset()),
]


# --------------------------------------------------------------------------- pilhas
@dataclass
class GuardTotals:
    """Acumulado da trava de rede do LiteLLM em todas as subidas do proxy."""
    starts: int = 0
    armed_pids: int = 0
    allowed: Counter = field(default_factory=Counter)
    blocked: list[dict] = field(default_factory=list)
    external: list[dict] = field(default_factory=list)
    monitor_samples: int = 0
    orphans: list[int] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {"proxy_starts": self.starts, "armed_python_processes": self.armed_pids,
                "allowed_events": dict(sorted(self.allowed.items())), "blocked": self.blocked,
                "external_connections": self.external, "monitor_samples": self.monitor_samples,
                "orphans": self.orphans}


def derive_litellm_config(mock_port: int, dest: Path, *, ollama_port: int = OLLAMA_PORT,
                          callbacks: list[str] | None = None, rpm: bool = True,
                          router: dict[str, Any] | None = None) -> Path:
    cfg = yaml.safe_load(smoke.CONFIG.read_text(encoding="utf-8"))
    for deployment in cfg["model_list"]:
        port = mock_port if deployment["model_name"] == "mock_market" else ollama_port
        deployment["litellm_params"]["api_base"] = f"http://127.0.0.1:{port}/v1"
        if not rpm:
            deployment["litellm_params"].pop("rpm", None)
    cfg["router_settings"].update(router or {})
    if callbacks:
        cfg["litellm_settings"]["callbacks"] = callbacks
    dest.write_text(yaml.safe_dump(cfg, sort_keys=False), encoding="utf-8")
    return dest


@contextlib.contextmanager
def litellm_stack(totals: GuardTotals, keep: Path | None, *, flags: frozenset[str] = frozenset(),
                  callbacks: list[str] | None = None, gateway_id: str = "litellm"):
    """Mock + proxy do LiteLLM com as TRÊS camadas de trava do Marco 13. Qualquer violação encerra tudo e
    levanta NetworkViolation (depois de limpar)."""
    workdir = Path(tempfile.mkdtemp(prefix="litellm-compare-"))
    mock_port, proxy_port, sink_port = free_port(), free_port(), free_port()
    while len({mock_port, proxy_port, sink_port}) < 3:
        proxy_port, sink_port = free_port(), free_port()
    ports = {"mock": mock_port, "proxy": proxy_port, "sink": sink_port}
    dead = free_port() if DEAD_VARIANT in flags else None
    while dead in ports.values():
        dead = free_port()
    if dead:
        ports["dead"] = dead
    key = "sk-" + secrets.token_hex(16)
    log = workdir / "netguard.jsonl"
    config = derive_litellm_config(mock_port, workdir / "config.litellm.yaml", ollama_port=dead or OLLAMA_PORT,
                                   callbacks=callbacks, rpm=RPM_FLAG in flags,
                                   router=VARIANT_ROUTER.get(gateway_id))
    try:
        env = smoke.proxy_env(workdir, key, sink_port, log)
    except TiktokenCacheError as exc:
        shutil.rmtree(workdir, ignore_errors=True)
        raise StackError(f"cache do tiktoken nao verificavel (abortando; quem decide e o usuario): {exc}") from exc
    procs: list[subprocess.Popen] = []
    handles = [open(workdir / f"{n}.log", "wb") for n in ("mock", "proxy")]
    monitor: smoke.ConnectionMonitor | None = None
    violation: Exception | None = None
    try:
        smoke.guard_self_test(workdir, env)
        flags = subprocess.CREATE_NEW_PROCESS_GROUP if sys.platform == "win32" else 0
        mock = subprocess.Popen([sys.executable, "-m", "mock_upstream.server"], cwd=REPO_ROOT,
                                env=child_env(MOCK_UPSTREAM_PORT=str(mock_port)), stdout=handles[0],
                                stderr=subprocess.STDOUT, creationflags=flags)
        procs.append(mock)
        wait_health(f"http://127.0.0.1:{mock_port}", mock, workdir / "mock.log", MOCK_HEALTH_TIMEOUT_S)
        proxy = subprocess.Popen(
            [str(smoke.VENV_PYTHON), "-m", "litellm.proxy.proxy_cli", "--config", str(config), "--host", "127.0.0.1",
             "--port", str(proxy_port), "--num_workers", "1", "--telemetry", "False"],
            cwd=workdir, env=env, stdout=handles[1], stderr=subprocess.STDOUT, creationflags=flags)
        procs.append(proxy)
        monitor = smoke.ConnectionMonitor(proxy.pid)
        monitor.start()
        base = f"http://127.0.0.1:{proxy_port}"
        deadline = time.monotonic() + smoke.PROXY_HEALTH_TIMEOUT_S
        while True:
            smoke.check_violations(log, monitor)
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
        yield Target("litellm", base, f"http://127.0.0.1:{mock_port}", {"Authorization": f"Bearer {key}"},
                     gateway_id)
        monitor.finish()
        smoke.check_violations(log, monitor)
    except smoke.NetworkViolation as exc:
        violation = exc
        raise
    finally:
        if monitor:
            monitor._stop.set()
        for proc in reversed(procs):
            smoke.kill_tree(proc) if proc.args[0] != sys.executable else stop(proc)
        for h in handles:
            h.close()
        events = smoke.netguard_events(log)
        blocked = smoke.netguard_blocked(log)
        totals.starts += 1
        totals.armed_pids += len({e["pid"] for e in events if e["decision"] == "armed"})
        totals.allowed.update(f"{e['event']} -> {classify_target(str(e['target']), ports)}"
                              for e in events if e["decision"] == "allowed")
        totals.blocked += [{**b, "target": classify_target(str(b["target"]), ports)} for b in blocked]
        if monitor:
            totals.external += [{"pid": c["pid"], "remote": c["remote"], "state": c["state"]}
                                for c in monitor.external()]
            totals.monitor_samples += monitor.samples
        time.sleep(1)
        totals.orphans += [p.pid for p in procs if smoke.pid_alive(p.pid)]
        if keep and (blocked or violation):
            shutil.copy(log, keep / f"netguard-VIOLATION-{totals.starts}.jsonl")
        shutil.rmtree(workdir, ignore_errors=True)


@contextlib.contextmanager
def own_stack(flags: frozenset[str] = frozenset()):
    workdir = Path(tempfile.mkdtemp(prefix="gateway-compare-"))
    gateway_port, mock_port = free_ports()
    dead = free_port() if DEAD_VARIANT in flags else None
    raw = build_runtime_config(DEMO_CONFIG, gateway_port=gateway_port, mock_port=mock_port, workdir=workdir,
                               fast=False)
    if RPM_FLAG not in flags:
        for p in raw["providers"]:
            if p["type"] == "mock":
                p["rate_limit"] = {"rpm": None, "tpm": None}
    if dead:
        for p in raw["providers"]:
            if p["type"] == "ollama":
                p["base_url"] = f"http://127.0.0.1:{dead}"
                p["warmup"] = False
    path = workdir / "config.yaml"
    path.write_text(yaml.safe_dump(raw, sort_keys=False), encoding="utf-8")
    try:
        with start_stack(path, workdir, gateway_port, mock_port) as stack:
            yield Target("proprio", stack.gateway_url, stack.mock_url, gateway_id="proprio")
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


# --------------------------------------------------------------------------- execução
async def warm_discard(target: Target, client: httpx.AsyncClient) -> None:
    """Requisição descartada (inicialização preguiçosa do proxy; não toca o mock)."""
    scene = Scene(target, client)
    if target.kind == "proprio":
        await scene.ask(label="aquecimento", headers={"x-gateway-force-provider": "ollama_qwen"}, track=False)
    else:
        await scene.ask(label="aquecimento", model="ollama_qwen", track=False)


async def run_scene_async(target: Target, scene_id: str, fn: Callable[[Scene], Awaitable[None]],
                          title: str) -> dict[str, Any]:
    async with httpx.AsyncClient() as client:
        await warm_discard(target, client)
        scene = Scene(target, client)
        await fn(scene)
        stats = await scene.mock_stats()
        if target.kind == "proprio":
            with contextlib.suppress(httpx.HTTPError, ValueError):
                scene.extra["own_status"] = (await client.get(f"{target.base}/status", timeout=10)).json()
        return {"scene": scene_id, "title": title, "gateway": target.gateway_id, "requests": scene.requests,
                "mock": stats, "time_to_mock_s": scene.time_to_mock_s, "extra": scene.extra}


def warm_ollama() -> None:
    """Aquece os dois modelos locais direto no Ollama e DESCARTA essa rodada."""
    for model in ("qwen2.5:3b", "llama3.2:3b"):
        httpx.post(f"http://127.0.0.1:{OLLAMA_PORT}/v1/chat/completions", timeout=300,
                   json={"model": model, "messages": [{"role": "user", "content": "hi"}], "max_tokens": 1}
                   ).raise_for_status()


def run_all(run_id: str, gateways: tuple[str, ...], only: set[str] | None, reps: int) -> int:
    out = RESULTS_ROOT / run_id
    out.mkdir(parents=True, exist_ok=True)
    if (reason := smoke.ollama_ready()) or ("litellm" in gateways and (reason := smoke.proxy_available())):
        print(f"erro: {reason}", file=sys.stderr)
        return 2
    totals = GuardTotals()
    warm_ollama()
    runs: list[dict[str, Any]] = []
    started = time.strftime("%Y-%m-%dT%H:%M:%S")
    try:
        for gateway in gateways:
            for scene_id, title, fn, flags in SCENES:
                if only and scene_id not in only:
                    continue
                if gateway in VARIANT_SCENES and scene_id not in VARIANT_SCENES[gateway]:
                    continue
                for rep in range(1, reps + 1):
                    stack = (own_stack(flags) if gateway == "proprio"
                             else litellm_stack(totals, out, flags=flags, gateway_id=gateway))
                    with stack as target:
                        run = asyncio.run(run_scene_async(target, scene_id, fn, title))
                    run["rep"] = rep
                    runs.append(run)
                    (out / gateway).mkdir(exist_ok=True)
                    (out / gateway / f"{scene_id}-r{rep}.json").write_text(
                        json.dumps(run, indent=2, ensure_ascii=False), encoding="utf-8")
                    print(f"[{gateway}] cena {scene_id} rep {rep}: {len(run['requests'])} req, mock recebeu "
                          f"{run['mock']['received']}, tempo ate voltar {run['time_to_mock_s']}", flush=True)
    except smoke.NetworkViolation as exc:
        (out / "netguard.json").write_text(json.dumps(totals.as_dict(), indent=2), encoding="utf-8")
        print(f"\nPARADO pela trava de rede: {exc}", file=sys.stderr)
        return 3
    except StackError as exc:
        print(f"erro: {exc}", file=sys.stderr)
        return 2
    litellm_version = next((r["headers"]["x-litellm-version"] for run in runs
                            if run["gateway"].startswith("litellm") for r in run["requests"]
                            if r.get("headers", {}).get("x-litellm-version")), None)
    meta = {"run_id": run_id, "started": started, "platform": platform.platform(), "python": platform.python_version(),
            "litellm_version": litellm_version, "reps": reps, "gateways": list(gateways),
            "litellm_variants": {k: v for k, v in VARIANT_ROUTER.items() if k in gateways},
            "scenes": [s[0] for s in SCENES if not only or s[0] in only]}
    (out / "meta.json").write_text(json.dumps(meta, indent=2), encoding="utf-8")
    (out / "results.json").write_text(json.dumps({"meta": meta, "runs": runs}, indent=2, ensure_ascii=False),
                                      encoding="utf-8")
    (out / "netguard.json").write_text(json.dumps(totals.as_dict(), indent=2), encoding="utf-8")
    print_guard(totals)
    return 1 if totals.blocked or totals.external or totals.orphans else 0


def print_guard(totals: GuardTotals) -> None:
    print("\n=== Trava de rede do LiteLLM (todas as subidas do proxy) ===")
    print(f"subidas do proxy: {totals.starts}; processos Python com a trava armada: {totals.armed_pids}; "
          f"amostras do monitor externo: {totals.monitor_samples}")
    print("eventos PERMITIDOS (so loopback):")
    for name, count in sorted(totals.allowed.items()):
        print(f"    {count:>6}  {name}")
    print(f"eventos BLOQUEADOS: {len(totals.blocked)}")
    for b in totals.blocked:
        print(f"    BLOQUEADO {b['event']} -> {b['target']} ({b['decision']})")
    print(f"conexoes fora do loopback (monitor externo): {len(totals.external)}")
    print(f"processos restantes: {totals.orphans or 'nenhum'}")


# --------------------------------------------------------------------------- sondas de recursos
PROBE_PATHS = ("/health/liveliness", "/health/readiness", "/health", "/metrics", "/metrics/", "/v1/models",
               "/model/info", "/key/generate", "/global/spend", "/spend/logs", "/routes", "/cooldown")


async def probe_litellm(target: Target) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    async with httpx.AsyncClient() as client:
        scene = Scene(target, client)
        await scene.ask(label="trafego para gerar metricas", model="ollama_qwen", track=False)
        for path in PROBE_PATHS:
            resp = await client.get(f"{target.base}{path}", headers=target.auth, timeout=30)
            ctype = resp.headers.get("content-type", "")
            body = resp.text
            row: dict[str, Any] = {"path": path, "status": resp.status_code, "content_type": ctype.split(";")[0]}
            if path.startswith("/metrics") and resp.status_code == 200:
                row["metric_families"] = sorted({line.split()[2] for line in body.splitlines()
                                                 if line.startswith("# TYPE ")})
            else:
                row["body_head"] = body[:300]
            rows.append(row)
    return rows


async def probe_own(target: Target) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    async with httpx.AsyncClient() as client:
        scene = Scene(target, client)
        await scene.ask(label="trafego", headers={"x-gateway-force-provider": "ollama_qwen"}, track=False)
        for path in ("/health", "/status", "/metrics"):
            resp = await client.get(f"{target.base}{path}", timeout=30)
            row: dict[str, Any] = {"path": path, "status": resp.status_code}
            if path == "/metrics":
                row["metric_families"] = sorted({line.split()[2] for line in resp.text.splitlines()
                                                 if line.startswith("# TYPE ")})
            else:
                row["body_head"] = resp.text[:300]
            rows.append(row)
    return rows


def run_probes(run_id: str) -> int:
    out = RESULTS_ROOT / run_id
    out.mkdir(parents=True, exist_ok=True)
    if reason := (smoke.ollama_ready() or smoke.proxy_available()):
        print(f"erro: {reason}", file=sys.stderr)
        return 2
    totals = GuardTotals()
    result: dict[str, Any] = {}
    try:
        with own_stack(frozenset()) as target:
            result["proprio"] = asyncio.run(probe_own(target))
        with litellm_stack(totals, out) as target:
            result["litellm_config_only"] = asyncio.run(probe_litellm(target))
        try:
            with litellm_stack(totals, out, callbacks=["prometheus"]) as target:
                result["litellm_prometheus_callback"] = asyncio.run(probe_litellm(target))
        except StackError as exc:  # ex.: ModuleNotFoundError de prometheus_client no ambiente travado por hash
            lines = [line for line in str(exc).splitlines() if line.strip()]
            error = next((x for x in reversed(lines) if "Error" in x), lines[-1] if lines else "?")
            result["litellm_prometheus_callback"] = {"startup_error": error[:300]}
    except smoke.NetworkViolation as exc:
        print(f"\nPARADO pela trava de rede: {exc}", file=sys.stderr)
        return 3
    (out / "probes.json").write_text(json.dumps(result, indent=2, ensure_ascii=False), encoding="utf-8")
    (out / "netguard-probes.json").write_text(json.dumps(totals.as_dict(), indent=2), encoding="utf-8")
    print_guard(totals)
    return 1 if totals.blocked or totals.external or totals.orphans else 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--gateways", default=",".join(GATEWAYS), help="proprio,litellm")
    parser.add_argument("--scenes", default="", help="lista de cenas separadas por virgula (padrao: todas)")
    parser.add_argument("--reps", type=int, default=REPS)
    parser.add_argument("--probes", action="store_true", help="so as sondas de recursos (metricas, endpoints)")
    args = parser.parse_args()
    if args.probes:
        return run_probes(args.run_id)
    gateways = tuple(g for g in args.gateways.split(",") if g)
    unknown = set(gateways) - set(GATEWAYS)
    if unknown:
        print(f"gateway desconhecido: {sorted(unknown)}", file=sys.stderr)
        return 2
    only = {s for s in args.scenes.split(",") if s} or None
    return run_all(args.run_id, gateways, only, args.reps)


if __name__ == "__main__":
    sys.exit(main())
