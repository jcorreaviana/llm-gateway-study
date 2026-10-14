"""Runner da avaliação: coleta as respostas de UM provedor local por vez, via gateway.

    python -m evalkit.run --provider ollama_qwen --run-id <id> [--resume] [--limit N]

O que esta fase mede: Qwen (qwen2.5:3b) contra Llama (llama3.2:3b), via gateway, com provedor forçado
(`x-gateway-force-provider`). NÃO mede a perda em relação a um modelo de mercado.

Nunca fala com a Anthropic: o provedor `anthropic` fica fora da chain (e desabilitado), os processos
filhos sobem com ENABLE_REAL_API=0 e ANTHROPIC_API_KEY vazia, e este módulo não importa nenhum cliente
dela. Um provedor por vez (os modelos não se alternam na VRAM) e os prompts vão em sequência, sem
concorrência, para a latência medida ser limpa.

Saída em `eval/results/<run_id>/` (ignorado pelo git):
- `<provider>.jsonl`: um resultado por prompt (`prompt_id`, `task_type`, `status`, `response`,
  `latency_ms`, `usage`, `request_id`, `error` quando houver). `status` 0 = sem resposta HTTP (timeout etc.).
- `meta.json`: data, modelos, versão do Ollama, hash do dataset, hash do config e commit do repositório.
- `gateway/<provider>/`: config derivado, logs dos processos, budget e recorder do gateway.

Baseline futuro (NÃO implementado aqui): `eval/baseline/<prompt_id>.json`, ignorado pelo git, com a
resposta de um modelo de mercado para cada prompt, para uma fase separada e autorizada.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import hashlib
import json
import os
import re
import statistics
import subprocess
import sys
import time
from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import yaml
from pydantic import ValidationError

from evalkit.schema import TASK_TYPES, EvalPrompt
from evalkit.validate_dataset import EVAL_CONFIG, PROMPTS_DIR, DatasetError, dataset_hash, load_dataset
from gateway.config import PROJECT_ROOT, AppConfig, load_config
from scripts._stack import StackError, free_ports, start_stack

RESULTS_ROOT = PROJECT_ROOT / "eval" / "results"
REQUEST_TIMEOUT_S = 180  # acima do timeout_s do provedor (120), para o gateway responder primeiro
WARMUP_PROMPT = "Diga olá em uma palavra."
RUN_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
SCOPE = (
    "qwen2.5:3b contra llama3.2:3b, via gateway, com provedor forçado; NÃO mede a perda em relação a um "
    "modelo de mercado. Nenhuma chamada à Anthropic."
)


class PreflightError(Exception):
    """Pré-requisito ausente ou inseguro: mensagem clara, sem traceback."""


@dataclass
class Options:
    provider: str
    run_id: str
    resume: bool = False
    limit: int | None = None
    config_path: Path = EVAL_CONFIG
    dataset_dir: Path = PROMPTS_DIR
    results_root: Path = RESULTS_ROOT


# --------------------------------------------------------------------------- pré-checagens
def guard_environment() -> None:
    """Fase sem Anthropic: recusa `ENABLE_REAL_API` ligada e zera a chave NESTE processo (os filhos já
    sobem com os dois neutralizados por `child_env`)."""
    if os.environ.get("ENABLE_REAL_API", "").strip().lower() in {"1", "true", "yes", "on"}:
        raise PreflightError(
            "ENABLE_REAL_API está ligada no ambiente. A avaliação não usa a Anthropic; "
            "desligue com ENABLE_REAL_API=0 e rode de novo."
        )
    os.environ["ENABLE_REAL_API"] = "0"
    os.environ["ANTHROPIC_API_KEY"] = ""


def check_no_anthropic(cfg: AppConfig) -> None:
    """O provedor `anthropic` nunca entra na cadeia de avaliação, e não pode estar habilitado."""
    by_name = {p.name: p for p in cfg.providers}
    in_chain = [n for n in cfg.chain if by_name[n].type == "anthropic"]
    if in_chain:
        raise PreflightError(
            f"o provedor anthropic está na chain ({', '.join(in_chain)}): a Fase 3 não usa a Anthropic"
        )
    enabled = [p.name for p in cfg.providers if p.type == "anthropic" and p.enabled]
    if enabled:
        raise PreflightError(f"o provedor anthropic está habilitado ({', '.join(enabled)}): use enabled: false")
    if not cfg.chain:  # o schema já exige, mas não custa nada deixar o motivo explícito
        raise PreflightError("a chain está vazia")


def check_provider(cfg: AppConfig, provider: str) -> None:
    by_name = {p.name: p for p in cfg.providers}
    if provider not in cfg.chain:
        raise PreflightError(f"provedor '{provider}' não está na chain do config de avaliação ({', '.join(cfg.chain)})")
    p = by_name[provider]
    if p.type != "ollama" or not p.enabled:
        raise PreflightError(
            f"provedor '{provider}' deve ser um Ollama habilitado (type={p.type}, enabled={p.enabled})"
        )
    if not cfg.gateway.force_provider_enabled:
        raise PreflightError("gateway.force_provider_enabled deve ser true no config de avaliação")


async def check_ollama(client: httpx.AsyncClient, cfg: AppConfig, provider: str) -> str:
    """Confirma que o Ollama responde e que o modelo existe (nunca baixa nada). Devolve a versão."""
    p = cfg.provider(provider)
    url = str(p.base_url).rstrip("/")
    try:
        version = (await client.get(f"{url}/api/version")).json().get("version")
        resp = await client.get(f"{url}/api/tags")
        resp.raise_for_status()
        installed = {m.get("name") for m in resp.json().get("models", [])}
    except (httpx.HTTPError, ValueError, AttributeError) as e:
        raise PreflightError(
            f"o Ollama não respondeu em {url} ({type(e).__name__}). Deixe-o rodando (ollama serve) e tente de novo."
        ) from e
    if p.model not in installed and f"{p.model}:latest" not in installed:
        raise PreflightError(
            f"modelo ausente no Ollama: {p.model}. O runner nunca baixa modelos; rode: ollama pull {p.model}"
        )
    return str(version)


def make_ollama_client() -> httpx.AsyncClient:
    return httpx.AsyncClient(timeout=5)


def make_gateway_client(base_url: str) -> httpx.AsyncClient:
    return httpx.AsyncClient(base_url=base_url, timeout=REQUEST_TIMEOUT_S)


# --------------------------------------------------------------------------- config, hashes e meta
def file_hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes().replace(b"\r\n", b"\n")).hexdigest()


def git_info() -> dict[str, Any]:
    def git(*args: str) -> str | None:
        try:
            out = subprocess.run(["git", *args], cwd=PROJECT_ROOT, capture_output=True, text=True, timeout=10)
        except (OSError, subprocess.SubprocessError):
            return None
        return out.stdout.strip() if out.returncode == 0 else None

    commit = git("rev-parse", "HEAD")
    status = git("status", "--porcelain")
    return {"git_commit": commit, "git_dirty": bool(status) if status is not None else None}


def build_runtime_config(
    base: Path, *, provider: str, gateway_port: int, mock_port: int, workdir: Path
) -> dict[str, Any]:
    """Cópia do config de avaliação com o que precisa ser dinâmico: portas livres, budget e recorder no
    diretório do run, e chain só com o provedor medido (os outros modelos não são carregados na VRAM)."""
    raw = yaml.safe_load(base.read_text(encoding="utf-8"))
    raw["gateway"]["port"] = gateway_port
    raw["mock_upstream"]["port"] = mock_port
    raw["budget"]["path"] = str(workdir / "budget.json")
    raw["recorder"]["path"] = str(workdir / "requests.jsonl")
    raw["chain"] = [provider]
    for p in raw["providers"]:
        if p["name"] != provider:
            p["warmup"] = False if p["type"] == "ollama" else p.get("warmup", False)
    check_no_anthropic(AppConfig.model_validate(raw))
    return raw


def write_json_atomic(path: Path, data: dict[str, Any]) -> None:
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def update_meta(run_dir: Path, *, opts: Options, model: str, ollama_version: str, dataset_sha: str, config_sha: str,
                total: int, finished: bool, results: int | None = None) -> dict[str, Any]:
    """Cria/atualiza `meta.json`. Dois provedores no mesmo run_id compartilham dataset e config; se eles
    mudaram, os resultados deixam de ser comparáveis e o runner recusa."""
    path = run_dir / "meta.json"
    meta: dict[str, Any] = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    if meta and (meta.get("dataset_hash") != dataset_sha or meta.get("config_hash") != config_sha):
        raise PreflightError(
            f"o run '{opts.run_id}' já existe com outro dataset ou config (hash diferente): use outro --run-id"
        )
    now = datetime.now(UTC).isoformat(timespec="seconds")
    meta.setdefault("run_id", opts.run_id)
    meta.setdefault("created_at", now)
    meta.setdefault("scope", SCOPE)
    meta.update(dataset_hash=dataset_sha, config_hash=config_sha, ollama_version=ollama_version, **git_info())
    entry = meta.setdefault("providers", {}).setdefault(opts.provider, {})
    resumed = opts.resume or entry.get("resumed", False)
    entry.update(model=model, prompts_selected=total, limit=opts.limit, resumed=resumed)
    entry.setdefault("started_at", now)
    if finished:
        entry["finished_at"] = now
        entry["results"] = results
    write_json_atomic(path, meta)
    return meta


# --------------------------------------------------------------------------- seleção e resultados
def select_prompts(prompts: list[EvalPrompt], limit: int | None) -> list[EvalPrompt]:
    """Todos os prompts, ou (com `limit`) uma amostra que alterna as categorias; mantém a ordem do dataset."""
    if limit is None or limit >= len(prompts):
        return list(prompts)
    queues: dict[str, list[EvalPrompt]] = defaultdict(list)
    for p in prompts:
        queues[p.task_type].append(p)
    chosen: list[EvalPrompt] = []
    while len(chosen) < limit and any(queues.values()):
        for cat in TASK_TYPES:
            if queues[cat] and len(chosen) < limit:
                chosen.append(queues[cat].pop(0))
    keep = {p.id for p in chosen}
    return [p for p in prompts if p.id in keep]


def read_results(path: Path) -> list[dict[str, Any]]:
    """Resultados já gravados. Uma linha final truncada (run interrompido no meio da escrita) é descartada
    e o arquivo é reescrito sem ela, para o `--resume` poder acrescentar com segurança."""
    if not path.exists():
        return []
    good: list[dict[str, Any]] = []
    dirty = False
    for raw in path.read_text(encoding="utf-8").splitlines():
        if not raw.strip():
            continue
        try:
            rec = json.loads(raw)
        except json.JSONDecodeError:
            dirty = True
            continue
        if isinstance(rec, dict) and "prompt_id" in rec:
            good.append(rec)
        else:
            dirty = True
    if dirty:
        path.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in good), encoding="utf-8")
    return good


def percentile50(values: list[float]) -> float | None:
    return statistics.median(values) if values else None


def summarize(records: list[dict[str, Any]]) -> dict[str, Any]:
    ok = [r for r in records if r.get("status") == 200 and not r.get("error")]
    return {
        "total": len(records),
        "ok": len(ok),
        "errors": len(records) - len(ok),
        "latency_p50_ms": percentile50([float(r["latency_ms"]) for r in ok]),
    }


def format_summary(provider: str, model: str, summary: dict[str, Any]) -> str:
    p50 = summary["latency_p50_ms"]
    p50_text = f"{p50:.0f} ms" if p50 is not None else "-"
    return (f"{provider} ({model}): {summary['total']} resultados, {summary['ok']} ok, "
            f"{summary['errors']} com erro, latência p50 {p50_text}")


async def ask(gw: httpx.AsyncClient, prompt: EvalPrompt, provider: str, perf: Callable[[], float]) -> dict[str, Any]:
    """Um prompt, forçando o provedor. Timeout, erro de conexão e 4xx/5xx viram um resultado com `error`."""
    start = perf()
    base: dict[str, Any] = {"prompt_id": prompt.id, "task_type": prompt.task_type, "provider": provider}
    try:
        resp = await gw.post("/v1/chat/completions", json=prompt.request_body(),
                             headers={"x-gateway-force-provider": provider})
    except httpx.HTTPError as e:
        latency = (perf() - start) * 1000
        return {**base, "status": 0, "response": None, "latency_ms": round(latency, 1), "usage": None,
                "request_id": None, "error": f"{type(e).__name__}: {e}".strip(": ")}
    latency = (perf() - start) * 1000
    rec: dict[str, Any] = {**base, "status": resp.status_code, "response": None, "latency_ms": round(latency, 1),
                           "usage": None, "request_id": resp.headers.get("x-gateway-request-id")}
    try:
        data = resp.json()
    except ValueError:
        data = None
    if resp.status_code == 200 and isinstance(data, dict):
        try:
            choice = data["choices"][0]
            rec["response"] = choice["message"]["content"] or ""
            rec["finish_reason"] = choice.get("finish_reason")
            rec["usage"] = data.get("usage")
            served = resp.headers.get("x-gateway-provider")
            if served and served != provider:
                rec["error"] = f"atendido por outro provedor: {served}"
        except (KeyError, IndexError, TypeError):
            rec["error"] = "resposta 200 sem choices[0].message.content"
    else:
        err = data.get("error") if isinstance(data, dict) else None
        message = err.get("message") if isinstance(err, dict) else None
        rec["error"] = message or (resp.text[:300] or f"HTTP {resp.status_code}")
    return rec


async def collect(
    gw: httpx.AsyncClient,
    prompts: list[EvalPrompt],
    provider: str,
    out_path: Path,
    *,
    resume: bool,
    perf: Callable[[], float] = time.perf_counter,
    out: Callable[[str], None] = print,
) -> list[dict[str, Any]]:
    """Warm-up descartado e prompts em SEQUÊNCIA; grava (e descarrega) cada resultado assim que chega.
    Devolve todos os resultados do arquivo (incluindo os de um run anterior, no caso de --resume)."""
    existing = read_results(out_path) if resume else []
    done = {r["prompt_id"] for r in existing}
    pending = [p for p in prompts if p.id not in done]
    if done:
        out(f"  --resume: {len(done)} prompt(s) já gravado(s) serão pulados; {len(pending)} restante(s).")
    if not pending:
        return existing
    out_path.parent.mkdir(parents=True, exist_ok=True)
    warm = EvalPrompt.model_validate({
        "id": "instruction-00", "task_type": "instruction", "lang": "pt-BR",
        "messages": [{"role": "user", "content": WARMUP_PROMPT}], "params": {"temperature": 0, "max_tokens": 8},
        "check": {"type": "constraints", "spec": {"rules": [{"kind": "max_words", "value": 50}]}}})
    warm_rec = await ask(gw, warm, provider, perf)  # descartado
    if warm_rec.get("error"):
        out(f"  aviso: o warm-up falhou ({warm_rec['error']}); a medição segue.")
    with open(out_path, "a", encoding="utf-8") as f:
        for i, prompt in enumerate(pending, 1):
            rec = await ask(gw, prompt, provider, perf)
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
            f.flush()
            state = "ok" if not rec.get("error") else f"erro ({rec['error'][:60]})"
            out(f"  [{i}/{len(pending)}] {prompt.id}: {state}, {rec['latency_ms']:.0f} ms")
            existing.append(rec)
    return existing


# --------------------------------------------------------------------------- execução
async def run_session(gateway_url: str, prompts: list[EvalPrompt], opts: Options, out_path: Path,
                      out: Callable[[str], None]) -> list[dict[str, Any]]:
    async with make_gateway_client(gateway_url) as gw:
        return await collect(gw, prompts, opts.provider, out_path, resume=opts.resume, out=out)


def execute(opts: Options, out: Callable[[str], None] = print) -> dict[str, Any]:
    """Pré-checagens, pilha de processos, coleta e meta. Devolve o resumo."""
    if not RUN_ID_RE.match(opts.run_id):
        raise PreflightError("--run-id inválido: use letras, números, '.', '_' ou '-' (até 64 caracteres)")
    guard_environment()
    try:
        cfg = load_config(opts.config_path)
    except (OSError, ValidationError, yaml.YAMLError) as e:
        raise PreflightError(f"config de avaliação inválido ({opts.config_path.name}): {e}") from e
    check_no_anthropic(cfg)
    check_provider(cfg, opts.provider)
    try:
        dataset = load_dataset(opts.dataset_dir)
    except DatasetError as e:
        problems = "\n  ".join(e.problems)
        raise PreflightError(f"dataset inválido (rode python -m evalkit.validate_dataset):\n  {problems}") from e
    prompts = select_prompts(dataset.prompts, opts.limit)

    async def preflight() -> str:
        async with make_ollama_client() as client:
            return await check_ollama(client, cfg, opts.provider)

    ollama_version = asyncio.run(preflight())

    run_dir = opts.results_root / opts.run_id
    out_path = run_dir / f"{opts.provider}.jsonl"
    if out_path.exists() and out_path.stat().st_size and not opts.resume:
        raise PreflightError(f"{out_path.name} já existe em {run_dir}: use --resume para continuar ou outro --run-id")
    workdir = run_dir / "gateway" / opts.provider
    workdir.mkdir(parents=True, exist_ok=True)
    model = cfg.provider(opts.provider).model
    dataset_sha, config_sha = dataset_hash(opts.dataset_dir), file_hash(opts.config_path)
    meta_args: dict[str, Any] = {"opts": opts, "model": model, "ollama_version": ollama_version,
                                 "dataset_sha": dataset_sha, "config_sha": config_sha, "total": len(prompts)}
    update_meta(run_dir, finished=False, **meta_args)

    gateway_port, mock_port = free_ports()
    raw = build_runtime_config(opts.config_path, provider=opts.provider, gateway_port=gateway_port,
                               mock_port=mock_port, workdir=workdir)
    config_file = workdir / "config.runtime.yaml"
    config_file.write_text(yaml.safe_dump(raw, allow_unicode=True), encoding="utf-8")
    out(f"Subindo o gateway (127.0.0.1:{gateway_port}); a chain é só {opts.provider} ({model}). Sem Anthropic.")
    out("Aviso: o warm-up carrega o modelo e pode demorar.")
    with start_stack(config_file, workdir, gateway_port, mock_port) as stack:
        records = asyncio.run(run_session(stack.gateway_url, prompts, opts, out_path, out))
    summary = summarize(records)
    update_meta(run_dir, finished=True, results=summary["total"], **meta_args)
    out(format_summary(opts.provider, model, summary))
    return summary


def parse_args(argv: list[str] | None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Coleta as respostas de um provedor local via gateway (sem Anthropic).")
    p.add_argument("--provider", required=True, help="ollama_qwen ou ollama_llama (um por vez)")
    p.add_argument("--run-id", required=True, help="identificador do run (pasta em eval/results/)")
    p.add_argument("--resume", action="store_true", help="pula os prompt_id já gravados")
    p.add_argument("--limit", type=int, default=None, help="só N prompts (amostra alternando categorias)")
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    for stream in (sys.stdout, sys.stderr):
        with contextlib.suppress(AttributeError, ValueError):
            stream.reconfigure(encoding="utf-8", errors="replace")
    if args.limit is not None and args.limit < 1:
        print("erro: --limit deve ser >= 1", file=sys.stderr)
        return 2
    try:
        execute(Options(provider=args.provider, run_id=args.run_id, resume=args.resume, limit=args.limit))
        return 0
    except (PreflightError, StackError) as e:
        print(f"erro: {e}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("\nInterrompido; processos encerrados. Continue com --resume.", file=sys.stderr)
        return 130


if __name__ == "__main__":
    sys.exit(main())
