"""Demo reproduzível do gateway: sobe o mock e o gateway como processos reais e conta a história do
projeto em cenas (fallback, cooldown, circuit breaker, token bucket).

    python scripts/demo.py [--fast] [--no-color] [--skip-ollama-check]

Pré-requisito: Ollama rodando com `qwen2.5:3b` e `llama3.2:3b` (a demo nunca baixa modelos).
Nunca fala com a Anthropic: os processos filhos sobem com ENABLE_REAL_API=0, sem chave, e o provedor
`anthropic` fica desabilitado. Logs do recorder, budget e config derivado ficam num diretório
temporário, removido no final; `data/` e `logs/` do repositório não são tocados.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import itertools
import os
import re
import sys
import tempfile
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:  # `python scripts/demo.py` põe scripts/ (e não a raiz) em sys.path
    sys.path.insert(0, str(REPO_ROOT))

import httpx  # noqa: E402
import yaml  # noqa: E402
from prometheus_client.parser import text_string_to_metric_families  # noqa: E402

from gateway.config import AppConfig, load_config  # noqa: E402
from scripts._stack import Stack, StackError, free_ports, start_stack  # noqa: E402

DEMO_CONFIG = REPO_ROOT / "config.demo.yaml"
MAX_TOKENS = 20  # respostas curtas (<= 30)
RESPONSE_CHARS = 40
FAST_COOLDOWN_S = 2.0
MARGIN_S = 0.3  # folga ao esperar um prazo terminar

# Prompts curtos e genéricos (nenhum dado pessoal).
PROMPTS = [
    "Say hello in one word.",
    "Name a primary color.",
    "What is 2 plus 2?",
    "Give one word that means happy.",
    "Name a common fruit.",
    "Answer yes or no: is water wet?",
]

# POST /chaos substitui a configuração inteira: sempre enviamos o payload completo.
CHAOS_DEFAULTS: dict[str, Any] = {
    "mode": "normal",
    "retry_after_seconds": 1,
    "error_status": 500,
    "latency_ms": 0,
    "fail_every_n": None,
    "fail_status": 500,
}

Sleep = Callable[[float], Awaitable[None]]


DemoError = StackError  # falha esperada (pré-requisito ausente etc.): mensagem clara, sem traceback


# --------------------------------------------------------------------------- formatação
@dataclass
class Row:
    n: int
    scene: str
    status: int
    provider: str  # quem respondeu ("-" quando ninguém)
    fallback: str  # "provedor:motivo,..." ou ""
    latency_ms: float
    text: str  # resposta (ou mensagem de erro)
    retry_after: str | None = None
    providers: dict[str, str] | None = None  # error.providers do 503


def clip(text: str, limit: int = RESPONSE_CHARS) -> str:
    """Primeiros `limit` caracteres, em ASCII (o resto vira `?`, para a tabela alinhar em qualquer console),
    com espaços/quebras colapsados e sem caracteres de controle."""
    ascii_text = text.encode("ascii", "replace").decode("ascii")
    flat = re.sub(r"\s+", " ", "".join(c if c.isprintable() or c.isspace() else " " for c in ascii_text)).strip()
    return flat[:limit]


HEADERS = ["#", "cena", "status", "provedor", "fallback (provedor:motivo)", "latencia (ms)", "resposta (40 chars)"]
RIGHT_ALIGNED = {0, 2, 5}


def format_table(rows: list[Row]) -> str:
    """Tabela em ASCII puro (sem caracteres de caixa Unicode; seguro no console do Windows)."""
    cells = [
        [str(r.n), r.scene, str(r.status), r.provider, r.fallback or "-", f"{r.latency_ms:.0f}", clip(r.text)]
        for r in rows
    ]
    widths = [max(len(h), *(len(c[i]) for c in cells)) if cells else len(h) for i, h in enumerate(HEADERS)]

    def line(values: list[str]) -> str:
        pairs = enumerate(zip(values, widths, strict=True))
        parts = [v.rjust(w) if i in RIGHT_ALIGNED else v.ljust(w) for i, (v, w) in pairs]
        return "| " + " | ".join(parts) + " |"

    rule = "+-" + "-+-".join("-" * w for w in widths) + "-+"
    return "\n".join([rule, line(HEADERS), rule, *(line(c) for c in cells), rule])


def format_state(status: dict[str, Any]) -> str:
    """Uma linha com breaker, buckets e cooldown dos provedores habilitados (via GET /status)."""
    parts = []
    for name, p in status["providers"].items():
        if not p["enabled"]:
            continue
        br = p["breaker"]
        text = f"{name}: breaker={br['state']}({br['consecutive_failures']} falhas)"
        rpm = p["buckets"]["rpm"]
        if rpm:
            text += f" rpm={rpm['level']:.1f}/{rpm['capacity']:g}"
        if p["cooldown_remaining_s"]:
            text += f" cooldown={p['cooldown_remaining_s']:.1f}s"
        parts.append(text)
    return "estado: " + " | ".join(parts)


def format_stats(stats: dict[str, Any]) -> str:
    by_status = ", ".join(f"{k}:{v}" for k, v in stats["by_status"].items()) or "-"
    return f"/stats do mock: recebidas={stats['received']} por_status={{{by_status}}}"


def parse_metrics(text: str) -> tuple[dict[str, int], int, dict[str, int]]:
    """De `GET /metrics`: (requisições por provedor, total de fallbacks, fallbacks por motivo)."""
    per_provider: dict[str, int] = {}
    by_reason: dict[str, int] = {}
    total = 0
    for family in text_string_to_metric_families(text):
        for s in family.samples:
            if s.name == "gateway_requests_total":
                per_provider[s.labels["provider"]] = per_provider.get(s.labels["provider"], 0) + int(s.value)
            elif s.name == "gateway_fallback_total":
                total += int(s.value)
                key = f"{s.labels['from_provider']} -> {s.labels['to_provider']} ({s.labels['reason']})"
                by_reason[key] = by_reason.get(key, 0) + int(s.value)
    return per_provider, total, by_reason


def format_summary(metrics_text: str) -> str:
    per_provider, total, by_reason = parse_metrics(metrics_text)
    lines = ["Resumo (via GET /metrics)", "  requisicoes por provedor:"]
    for name, n in sorted(per_provider.items(), key=lambda kv: (-kv[1], kv[0])):
        label = "none (503/erro do gateway)" if name == "none" else name
        lines.append(f"    {label}: {n}")
    lines.append(f"  total de requisicoes: {sum(per_provider.values())}")
    lines.append(f"  total de fallbacks: {total}")
    for key, n in sorted(by_reason.items(), key=lambda kv: (-kv[1], kv[0])):
        lines.append(f"    {key}: {n}")
    return "\n".join(lines)


class Style:
    """Cores ANSI mínimas; desligadas com --no-color, NO_COLOR ou fora de um terminal."""

    def __init__(self, color: bool) -> None:
        self.color = color

    def _wrap(self, code: str, text: str) -> str:
        return f"\033[{code}m{text}\033[0m" if self.color else text

    def title(self, text: str) -> str:
        return self._wrap("1;36", text)

    def ok(self, text: str) -> str:
        return self._wrap("32", text)

    def warn(self, text: str) -> str:
        return self._wrap("33", text)

    def dim(self, text: str) -> str:
        return self._wrap("2", text)


# --------------------------------------------------------------------------- configuração
@dataclass(frozen=True)
class DemoSettings:
    rpm: int
    failure_threshold: int
    cooldown_s: float
    retry_after_s: int

    @classmethod
    def from_config(cls, cfg: AppConfig) -> DemoSettings:
        mock = cfg.provider("mock_market")
        if not mock.rate_limit or not mock.rate_limit.rpm:
            raise DemoError("config da demo: mock_market precisa de rate_limit.rpm (cena 'Limite local')")
        cooldown = mock.breaker.cooldown_s
        return cls(mock.rate_limit.rpm, mock.breaker.failure_threshold, cooldown, max(1, round(cooldown)))


def build_runtime_config(
    base: Path, *, gateway_port: int, mock_port: int, workdir: Path, fast: bool
) -> dict[str, Any]:
    """Cópia do config da demo com o que precisa ser dinâmico: portas livres, URL do mock, budget e
    recorder no diretório temporário (e cooldown mínimo com --fast). Validada pelo mesmo schema."""
    raw = yaml.safe_load(base.read_text(encoding="utf-8"))
    raw["gateway"]["port"] = gateway_port
    raw["mock_upstream"]["port"] = mock_port
    raw["budget"]["path"] = str(workdir / "data" / "budget.json")
    raw["recorder"]["path"] = str(workdir / "logs" / "requests.jsonl")
    for p in raw["providers"]:
        if p["type"] == "mock":
            p["base_url"] = f"http://127.0.0.1:{mock_port}"
            if fast:
                p.setdefault("breaker", {})["cooldown_s"] = FAST_COOLDOWN_S
    AppConfig.model_validate(raw)
    return raw


# --------------------------------------------------------------------------- pré-requisito: Ollama
def make_ollama_client() -> httpx.AsyncClient:
    return httpx.AsyncClient(timeout=5)


async def check_ollama(client: httpx.AsyncClient, cfg: AppConfig) -> None:
    """Confirma que o Ollama responde e que os modelos da config existem. Nunca baixa nada."""
    wanted: dict[str, set[str]] = {}
    for p in cfg.providers:
        if p.type == "ollama" and p.enabled:
            wanted.setdefault(str(p.base_url).rstrip("/"), set()).add(p.model)
    for url, models in wanted.items():
        try:
            resp = await client.get(f"{url}/api/tags")
            resp.raise_for_status()
            installed = {m.get("name") for m in resp.json().get("models", [])}
        except (httpx.HTTPError, ValueError) as e:
            raise DemoError(
                f"o Ollama nao respondeu em {url} ({type(e).__name__}).\n"
                "  Instale em https://ollama.com e deixe-o rodando (por exemplo: ollama serve), "
                "depois rode a demo de novo."
            ) from e
        missing = sorted(m for m in models if m not in installed and f"{m}:latest" not in installed)
        if missing:
            pulls = "\n".join(f"    ollama pull {m}" for m in missing)
            raise DemoError(
                f"modelo(s) ausente(s) no Ollama: {', '.join(missing)}.\n"
                f"  A demo nunca baixa modelos sozinha. Rode:\n{pulls}"
            )


# --------------------------------------------------------------------------- cenas
class Demo:
    """Conduz as cenas. `gw` e `mock` são AsyncClients (HTTP real na demo, ASGITransport nos testes);
    `sleep` e `perf` são injetáveis para os testes não dependerem do relógio."""

    def __init__(
        self,
        gw: httpx.AsyncClient,
        mock: httpx.AsyncClient,
        settings: DemoSettings,
        *,
        sleep: Sleep = asyncio.sleep,
        perf: Callable[[], float] = time.perf_counter,
        out: Callable[[str], None] = print,
        style: Style | None = None,
    ) -> None:
        self.gw, self.mock, self.settings = gw, mock, settings
        self._sleep, self._perf, self._out = sleep, perf, out
        self.style = style or Style(False)
        self.rows: list[Row] = []
        self._scene_rows: list[Row] = []
        self._scene = ""
        self._prompts = itertools.cycle(PROMPTS)

    # -- apoio
    def say(self, text: str = "") -> None:
        self._out(text)

    async def chaos(self, **kw: Any) -> None:
        r = await self.mock.post("/chaos", json={**CHAOS_DEFAULTS, **kw})  # payload sempre completo
        r.raise_for_status()

    async def reset_stats(self) -> None:
        (await self.mock.post("/stats/reset")).raise_for_status()

    async def status(self) -> dict[str, Any]:
        return (await self.gw.get("/status")).json()

    async def mock_market(self) -> dict[str, Any]:
        return (await self.status())["providers"]["mock_market"]

    async def stats(self) -> dict[str, Any]:
        return (await self.mock.get("/stats")).json()

    def begin(self, number: int, title: str, summary: str, slug: str) -> None:
        self._scene, self._scene_rows = slug, []
        self.say()
        self.say(self.style.title(f"=== Cena {number}: {title} ==="))
        self.say(self.style.dim(summary))

    async def ask(self, prompt: str | None = None, force: str | None = None) -> Row:
        body = {"messages": [{"role": "user", "content": prompt or next(self._prompts)}], "max_tokens": MAX_TOKENS}
        headers = {"x-gateway-force-provider": force} if force else {}
        start = self._perf()
        resp = await self.gw.post("/v1/chat/completions", json=body, headers=headers)
        latency_ms = (self._perf() - start) * 1000
        try:
            data = resp.json()
        except ValueError:
            data = {}
        error = data.get("error") if isinstance(data, dict) else None
        if resp.status_code == 200:
            text = data["choices"][0]["message"]["content"] or ""
        else:
            text = (error or {}).get("message") or resp.text
        row = Row(
            n=len(self.rows) + 1,
            scene=self._scene,
            status=resp.status_code,
            provider=resp.headers.get("x-gateway-provider", "-"),
            fallback=resp.headers.get("x-gateway-fallback-reason", ""),
            latency_ms=latency_ms,
            text=text,
            retry_after=resp.headers.get("retry-after"),
            providers=(error or {}).get("providers"),
        )
        self.rows.append(row)
        self._scene_rows.append(row)
        return row

    async def report(self, with_stats: bool = True) -> None:
        self.say(format_table(self._scene_rows))
        self.say(format_state(await self.status()))
        if with_stats:
            self.say(format_stats(await self.stats()))

    async def wait_for(self, what: str, seconds: float) -> None:
        if seconds > 0:
            self.say(self.style.dim(f"... aguardando {what} (~{seconds:.0f}s)"))
            await self._sleep(seconds + MARGIN_S)

    # -- cenas
    async def scene_normal(self) -> None:
        self.begin(1, "Normal", "O mock responde normalmente: as requisicoes sao atendidas pelo mock_market.", "normal")
        await self.reset_stats()
        await self.chaos(mode="normal")
        for _ in range(3):
            await self.ask()
        await self.report()

    async def scene_provider_rate_limit(self) -> None:
        s = self.settings
        self.begin(2, "Rate limit do provedor",
                   f"O mock devolve 429 com Retry-After={s.retry_after_s}s: fallback para o Qwen e cooldown "
                   "(as proximas requisicoes pulam o mock sem chama-lo).", "rate_limit")
        await self.reset_stats()
        await self.chaos(mode="rate_limit", retry_after_seconds=s.retry_after_s)
        for _ in range(3):
            await self.ask()
        await self.report()
        received = (await self.stats())["received"]
        self.say(f"confirmado: o mock recebeu {received} requisicao(oes) de 3; as demais foram puladas por cooldown.")
        await self.wait_for("o cooldown terminar", (await self.mock_market())["cooldown_remaining_s"])

    async def scene_provider_failure(self) -> None:
        n = self.settings.failure_threshold
        self.begin(3, "Falha do provedor",
                   f"O mock devolve 500: o breaker abre apos {n} falhas seguidas e o mock deixa de ser chamado.",
                   "server_error")
        await self.reset_stats()
        await self.chaos(mode="server_error", error_status=500)
        for _ in range(n):
            await self.ask()
        opened_at = (await self.stats())["received"]
        for _ in range(2):
            await self.ask()
        await self.report()
        final = (await self.stats())["received"]
        verdict = self.style.ok("OK") if final == opened_at else self.style.warn("ATENCAO")
        self.say(f"{verdict}: o mock tinha recebido {opened_at} requisicoes quando o breaker abriu e tem {final} "
                 "depois de mais 2 requisicoes (nao chegaram ao mock).")

    async def scene_recovery(self) -> None:
        self.begin(4, "Recuperacao",
                   "O mock volta ao normal; passado o cooldown o breaker vai a half_open, testa com uma sonda e fecha.",
                   "recovery")
        await self.reset_stats()
        await self.chaos(mode="normal")
        mm = await self.mock_market()
        if mm["breaker"]["state"] == "open":
            await self.ask()  # o mock ja esta saudavel, mas o breaker ainda nao deixa passar
            self.say("o mock esta saudavel, mas o breaker segue aberto ate o fim do cooldown.")
            remaining = (await self.mock_market())["breaker"]["open_remaining_s"] or 0
            await self.wait_for("o cooldown do breaker terminar", remaining)
        for _ in range(20):  # espera curta e limitada pelo half_open (o /status aplica a transicao por tempo)
            if (await self.mock_market())["breaker"]["state"] != "open":
                break
            await self._sleep(0.5)
        self.say(f"/status: mock_market breaker={(await self.mock_market())['breaker']['state']}")
        await self.ask()  # a sonda
        self.say(f"/status depois da sonda: mock_market breaker={(await self.mock_market())['breaker']['state']}")
        await self.ask()
        await self.report()

    async def scene_local_limit(self) -> None:
        self.begin(5, "Limite local",
                   f"Mais requisicoes que o balde de {self.settings.rpm} rpm do mock_market: "
                   "rate_limit_local e fallback para o Qwen, sem chamar o mock.", "local_limit")
        await self.reset_stats()
        await self.chaos(mode="normal")
        seen = False
        for _ in range(self.settings.rpm + 3):
            row = await self.ask()
            if seen:
                break  # uma requisicao a mais depois do primeiro rate_limit_local
            seen = "mock_market:rate_limit_local" in row.fallback
        await self.report()
        if not seen:
            self.say(self.style.warn("ATENCAO: o limite local nao apareceu (o balde reabasteceu durante a cena)."))

    async def scene_all_unavailable(self) -> None:
        self.begin(6, "Tudo indisponivel",
                   "Forcando um unico provedor (sem fallback) que esta indisponivel: 503 com o motivo por provedor.",
                   "unavailable")
        for name in ("mock_market", "anthropic"):
            row = await self.ask(force=name)
            self.say(f"forcando {name}: status={row.status} error.providers={row.providers} "
                     f"Retry-After={row.retry_after or '-'}")
        await self.report(with_stats=False)

    SCENES = (
        "scene_normal", "scene_provider_rate_limit", "scene_provider_failure",
        "scene_recovery", "scene_local_limit", "scene_all_unavailable",
    )

    async def run(self) -> None:
        for name in self.SCENES:
            await getattr(self, name)()
        self.say()
        self.say(format_summary((await self.gw.get("/metrics")).text))


async def run_session(stack: Stack, settings: DemoSettings, out: Callable[[str], None], style: Style) -> None:
    async with httpx.AsyncClient(base_url=stack.gateway_url, timeout=180) as gw, \
            httpx.AsyncClient(base_url=stack.mock_url, timeout=10) as mock:
        await Demo(gw, mock, settings, out=out, style=style).run()


# --------------------------------------------------------------------------- main
def parse_args(argv: list[str] | None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Demo do gateway com fallback (nenhuma chamada real a Anthropic).")
    p.add_argument("--fast", action="store_true", help="cooldowns e esperas minimos (so para teste)")
    p.add_argument("--skip-ollama-check", action="store_true", help="nao verifica o Ollama (nao recomendado)")
    p.add_argument("--no-color", action="store_true", help="sem cores ANSI")
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    for stream in (sys.stdout, sys.stderr):
        with contextlib.suppress(AttributeError, ValueError):
            stream.reconfigure(encoding="utf-8", errors="replace")
    color = not args.no_color and "NO_COLOR" not in os.environ and sys.stdout.isatty()
    if color and sys.platform == "win32":
        os.system("")  # habilita sequencias ANSI no console do Windows
    style = Style(color)
    try:
        base = load_config(DEMO_CONFIG)
        if not args.skip_ollama_check:
            async def preflight() -> None:
                async with make_ollama_client() as client:
                    await check_ollama(client, base)
            asyncio.run(preflight())
        print(style.title("Demo do gateway") + " - nenhuma chamada real a Anthropic (ENABLE_REAL_API=0, sem chave).")
        print("Aviso: o warm-up carrega os modelos locais e a primeira resposta local pode demorar.")
        with tempfile.TemporaryDirectory(prefix="llm-gateway-demo-") as tmp:
            workdir = Path(tmp)
            gateway_port, mock_port = free_ports()
            raw = build_runtime_config(DEMO_CONFIG, gateway_port=gateway_port, mock_port=mock_port,
                                       workdir=workdir, fast=args.fast)
            config_path = workdir / "config.demo.runtime.yaml"
            config_path.write_text(yaml.safe_dump(raw, allow_unicode=True), encoding="utf-8")
            settings = DemoSettings.from_config(AppConfig.model_validate(raw))
            print(f"Subindo mock (127.0.0.1:{mock_port}) e gateway (127.0.0.1:{gateway_port})...")
            with start_stack(config_path, workdir, gateway_port, mock_port) as stack:
                asyncio.run(run_session(stack, settings, print, style))
        print("\nDemo concluida; processos encerrados.")
        return 0
    except DemoError as e:
        print(f"erro: {e}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("\nInterrompido; processos encerrados.", file=sys.stderr)
        return 130


if __name__ == "__main__":
    sys.exit(main())
