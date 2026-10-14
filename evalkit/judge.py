"""Juiz local (Marco 11): um modelo de 3B julga respostas de modelos de 3B. Sem Anthropic.

    python -m evalkit.judge --run-id <id> --judge ollama_qwen|ollama_llama [--resume] [--limit N]

O juiz NÃO é tratado como verdade: este módulo só COLETA os julgamentos; `evalkit.judge_report` mede se eles
concordam com os scorers determinísticos e decide quanto confiar neles. Um juiz por vez (os dois modelos não
se alternam na VRAM): rode o comando duas vezes, uma para cada juiz.

- Pontual: nota 1 a 5 por critério (rubrica por `task_type`) e `overall`, para cada resposta avaliável.
- A resposta julgada é DADO: vai entre delimitadores e o prompt de sistema manda ignorar instruções dentro dela.
- O juiz recebe o enunciado, a resposta e, quando existirem, `reference_answer` e `key_points`. Nunca o
  `check` nem o resultado do scorer (`judge_prompts.judge_view` é o único filtro).
- `temperature` 0 e `max_tokens` baixo. JSON inválido: UMA nova tentativa; depois disso vira `judge_error`
  (o run continua).

Saída em `eval/results/<run_id>/judge/` (ignorado pelo git): `<juiz>.jsonl` (um julgamento por linha),
`<juiz>.meta.json` (modelo, hash do prompt do juiz, hash do dataset) e `gateway/<juiz>/` (processos).
A pasta é separada de propósito: um `*.jsonl` no topo do run seria lido como um provedor pelo `evalkit.score`.

Mesmas pré-checagens do runner: Ollama no ar, modelo existente (nunca baixa nada), `anthropic` fora da chain
e `ENABLE_REAL_API=0`. Os julgamentos passam pelo gateway, com o juiz como provedor forçado.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import re
import sys
import time
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import yaml
from pydantic import ValidationError

from evalkit.judge_prompts import (
    JUDGE_PROMPT_VERSION,
    RETRY_REMINDER,
    RUBRICS,
    SCALE_MAX,
    SCALE_MIN,
    judge_view,
    pointwise_messages,
    prompt_hash,
)
from evalkit.run import (
    RUN_ID_RE,
    PreflightError,
    build_runtime_config,
    check_no_anthropic,
    check_ollama,
    check_provider,
    file_hash,
    git_info,
    guard_environment,
    make_gateway_client,
    make_ollama_client,
    select_prompts,
    write_json_atomic,
)
from evalkit.schema import EvalPrompt
from evalkit.score import RESULTS_ROOT, ScoreError, load_run
from evalkit.scorers import as_text, classify_error
from evalkit.validate_dataset import EVAL_CONFIG, PROMPTS_DIR, DatasetError, dataset_hash, load_dataset
from gateway.config import load_config
from scripts._stack import StackError, free_ports, start_stack

JUDGE_MAX_TOKENS = 256
JUDGE_DIRNAME = "judge"
SCOPE = (
    "juiz local (modelo de 3B julgando modelo de 3B), via gateway, com provedor forçado; NÃO é verdade de "
    "referência: a confiança no juiz é medida contra os scorers determinísticos. Nenhuma chamada à Anthropic."
)

ChatFn = Callable[[list[dict[str, str]]], Awaitable[dict[str, Any]]]
Parser = Callable[[str], dict[str, Any] | None]


# --------------------------------------------------------------------------- parser tolerante
def json_objects(text: str) -> list[dict[str, Any]]:
    """Objetos JSON do texto, em ordem: o texto inteiro, ou todo objeto que começa em `{` (cercas de código e
    prosa antes/depois não atrapalham; objetos aninhados não são devolvidos de novo)."""
    raw = text or ""
    with contextlib.suppress(ValueError):
        whole = json.loads(raw.strip())
        if isinstance(whole, dict):
            return [whole]
    decoder = json.JSONDecoder()
    found: list[dict[str, Any]] = []
    skip_until = 0
    for m in re.finditer(r"\{", raw):
        if m.start() < skip_until:
            continue
        try:
            value, end = decoder.raw_decode(raw, m.start())
        except ValueError:
            continue
        if isinstance(value, dict):
            found.append(value)
            skip_until = end
    return found


_SCORE_RE = re.compile(r"^\s*([1-5])(?:\s*(?:/|de)\s*5)?\s*$")


def coerce_score(value: Any) -> int | None:
    """Nota inteira de 1 a 5 (aceita 4.0 e "4"/"4/5"). Fora da escala ou não inteira: None, nunca ajusta."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if SCALE_MIN <= value <= SCALE_MAX else None
    if isinstance(value, float):
        return int(value) if value.is_integer() and SCALE_MIN <= value <= SCALE_MAX else None
    if isinstance(value, str):
        m = _SCORE_RE.match(value)
        return int(m.group(1)) if m else None
    return None


def _mean_round(values: Sequence[int]) -> int:
    return int(sum(values) / len(values) + 0.5)


def _short(text: Any, limit: int = 300) -> str:
    return as_text(text).strip()[:limit]


def _pointwise_from_object(obj: dict[str, Any], criteria: Sequence[str]) -> dict[str, Any] | None:
    raw_scores = obj.get("scores")
    scores: dict[str, int] = {}
    if isinstance(raw_scores, dict):
        for key, value in raw_scores.items():
            n = coerce_score(value)
            if n is not None:
                scores[str(key)] = n
    overall = coerce_score(obj.get("overall"))
    derived = False
    if overall is None and "overall" not in obj and criteria and all(k in scores for k in criteria):
        overall, derived = _mean_round([scores[k] for k in criteria]), True
    if overall is None:
        return None
    return {"scores": scores, "overall": overall, "reason": _short(obj.get("reason")), "recovered": False,
            "overall_derived": derived}


def _recover_pointwise(text: str, criteria: Sequence[str]) -> dict[str, Any] | None:
    """JSON truncado (o `max_tokens` cortou no meio): ainda dá para ler `overall` e as notas já escritas."""
    scores = {}
    for key in criteria:
        m = re.search(rf'"{re.escape(key)}"\s*:\s*"?\s*([1-5])\b', text)
        if m:
            scores[key] = int(m.group(1))
    m = re.search(r'"overall"\s*:\s*"?\s*([0-9]+(?:\.[0-9]+)?)', text)
    overall = coerce_score(float(m.group(1))) if m else None
    derived = False
    if overall is None and m is None and criteria and all(k in scores for k in criteria):
        overall, derived = _mean_round([scores[k] for k in criteria]), True
    if overall is None:
        return None
    r = re.search(r'"reason"\s*:\s*"([^"]*)', text)
    return {"scores": scores, "overall": overall, "reason": _short(r.group(1)) if r else "", "recovered": True,
            "overall_derived": derived}


def parse_pointwise(text: str, criteria: Sequence[str]) -> dict[str, Any] | None:
    """`{"scores": {...}, "overall": n, "reason": "..."}` -> dict normalizado, ou None se irrecuperável."""
    objects = [o for o in json_objects(text) if "overall" in o or "scores" in o]
    if objects:
        return _pointwise_from_object(objects[0], criteria)
    return _recover_pointwise(text or "", criteria)


# --------------------------------------------------------------------------- chamada e nova tentativa
async def post_chat(
    gw: httpx.AsyncClient, provider: str, messages: list[dict[str, str]], *,
    max_tokens: int = JUDGE_MAX_TOKENS, perf: Callable[[], float] = time.perf_counter,
) -> dict[str, Any]:
    """Uma chamada ao gateway com o juiz como provedor forçado. Nunca levanta: erro vira `error`."""
    start = perf()

    def done(**kw: Any) -> dict[str, Any]:
        return {"content": None, "finish_reason": None, "request_id": None, "error": None,
                "latency_ms": round((perf() - start) * 1000, 1), **kw}

    try:
        resp = await gw.post(
            "/v1/chat/completions", headers={"x-gateway-force-provider": provider},
            json={"messages": messages, "temperature": 0, "max_tokens": max_tokens},
        )
    except httpx.HTTPError as e:
        return done(error=f"{type(e).__name__}: {e}".strip(": "))
    rid = resp.headers.get("x-gateway-request-id")
    try:
        data = resp.json()
    except ValueError:
        data = None
    if resp.status_code != 200 or not isinstance(data, dict):
        err = data.get("error") if isinstance(data, dict) else None
        message = err.get("message") if isinstance(err, dict) else None
        return done(request_id=rid, error=message or resp.text[:200] or f"HTTP {resp.status_code}")
    try:
        choice = data["choices"][0]
        served = resp.headers.get("x-gateway-provider")
        if served and served != provider:
            return done(request_id=rid, error=f"atendido por outro provedor: {served}")
        return done(content=choice["message"]["content"] or "", finish_reason=choice.get("finish_reason"),
                    request_id=rid)
    except (KeyError, IndexError, TypeError):
        return done(request_id=rid, error="resposta 200 sem choices[0].message.content")


async def run_judgment(chat: ChatFn, messages: list[dict[str, str]], parse: Parser) -> dict[str, Any]:
    """Uma chamada e, se o JSON for inválido, UMA nova tentativa (lembrete de formato). Depois disso é
    `judge_error`: o run continua. Erro de transporte (timeout, 5xx) também vira `judge_error`."""
    msgs = messages
    out: dict[str, Any] = {"attempts": 0, "latency_ms": 0.0, "raw": None, "finish_reason": None}
    for _ in range(2):
        res = await chat(msgs)
        out["attempts"] += 1
        out["latency_ms"] = round(out["latency_ms"] + float(res.get("latency_ms") or 0), 1)
        if res.get("error"):
            return {**out, "judge_error": True, "error_kind": "transport", "error": _short(res["error"], 200)}
        raw = res.get("content") or ""
        out.update(raw=raw, finish_reason=res.get("finish_reason"), request_id=res.get("request_id"))
        parsed = parse(raw)
        if parsed is not None:
            return {**out, **parsed, "judge_error": False, "error_kind": None, "error": None}
        msgs = [*messages, {"role": "assistant", "content": raw[:800] or "(vazio)"},
                {"role": "user", "content": RETRY_REMINDER}]
    return {**out, "judge_error": True, "error_kind": "invalid_json",
            "error": "resposta sem JSON válido depois de uma nova tentativa"}


# --------------------------------------------------------------------------- itens a julgar
@dataclass(frozen=True)
class Item:
    key: str
    kind: str  # "pointwise"
    prompt_id: str
    messages: list[dict[str, str]]
    meta: dict[str, Any] = field(default_factory=dict)
    criteria: tuple[str, ...] = ()


def response_index(results: dict[str, list[dict[str, Any]]]) -> dict[tuple[str, str], str]:
    """(provedor, prompt_id) -> texto, só das respostas AVALIÁVEIS (sem erro, sem resposta vazia)."""
    index = {}
    for provider, records in results.items():
        for rec in records:
            if classify_error(rec) is None:
                index[(provider, rec["prompt_id"])] = as_text(rec.get("response"))
    return index


def plan_items(prompts: dict[str, EvalPrompt], results: dict[str, list[dict[str, Any]]],
               ids: Sequence[str] | None = None) -> list[Item]:
    """Julgamentos a fazer, em ordem de prompt: um pontual por resposta avaliável."""
    index = response_index(results)
    wanted = sorted(ids if ids is not None else prompts)
    items: list[Item] = []
    for pid in wanted:
        prompt = prompts.get(pid)
        if prompt is None:
            continue
        view = judge_view(prompt)
        for provider in sorted(results):
            if (provider, pid) in index:
                items.append(Item(
                    key=f"pointwise|{pid}|{provider}", kind="pointwise", prompt_id=pid,
                    messages=pointwise_messages(view, index[(provider, pid)]),
                    meta={"target": provider}, criteria=tuple(RUBRICS[prompt.task_type]),
                ))
    return items


def parser_for(item: Item) -> Parser:
    return lambda text: parse_pointwise(text, item.criteria)


# --------------------------------------------------------------------------- coleta (retomável)
def read_judgments(path: Path) -> list[dict[str, Any]]:
    """Julgamentos já gravados. Linha final truncada (run interrompido) é descartada e o arquivo reescrito."""
    if not path.exists():
        return []
    good, dirty = [], False
    for raw in path.read_text(encoding="utf-8").splitlines():
        if not raw.strip():
            continue
        try:
            rec = json.loads(raw)
        except json.JSONDecodeError:
            dirty = True
            continue
        if isinstance(rec, dict) and "key" in rec:
            good.append(rec)
        else:
            dirty = True
    if dirty:
        path.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in good), encoding="utf-8")
    return good


async def collect(chat: ChatFn, items: list[Item], judge: str, out_path: Path, *, resume: bool,
                  out: Callable[[str], None] = print) -> list[dict[str, Any]]:
    """Julgamentos em SEQUÊNCIA; grava (e descarrega) cada um assim que chega. Com `resume`, pula as chaves já
    gravadas (inclusive as com `judge_error`: não há nova tentativa automática entre execuções)."""
    existing = read_judgments(out_path) if resume else []
    done = {r["key"] for r in existing}
    pending = [it for it in items if it.key not in done]
    if done:
        out(f"  --resume: {len(done)} julgamento(s) já gravado(s) serão pulados; {len(pending)} restante(s).")
    if not pending:
        return existing
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "a", encoding="utf-8") as f:
        for i, item in enumerate(pending, 1):
            result = await run_judgment(chat, item.messages, parser_for(item))
            rec = {"key": item.key, "kind": item.kind, "prompt_id": item.prompt_id, "judge": judge,
                   **item.meta, **result}
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
            f.flush()
            state = f"judge_error ({result['error_kind']})" if result["judge_error"] else describe(rec)
            out(f"  [{i}/{len(pending)}] {item.key}: {state}, {result['latency_ms']:.0f} ms")
            existing.append(rec)
    return existing


def describe(rec: dict[str, Any]) -> str:
    return f"nota {rec['overall']}"


def summarize(records: list[dict[str, Any]]) -> dict[str, int]:
    errors = sum(1 for r in records if r.get("judge_error"))
    return {"total": len(records), "ok": len(records) - errors, "judge_errors": errors,
            "retried": sum(1 for r in records if r.get("attempts", 1) > 1)}


# --------------------------------------------------------------------------- meta e execução
def judge_dir(run_dir: Path) -> Path:
    return run_dir / JUDGE_DIRNAME


def update_meta(path: Path, *, judge: str, model: str, ollama_version: str, run_id: str, dataset_sha: str,
                config_sha: str, resumed: bool, finished: bool, summary: dict[str, int] | None = None) -> None:
    """Cria/atualiza `<juiz>.meta.json`. Com outro hash de prompt do juiz ou de dataset, os julgamentos deixam de
    ser comparáveis e o runner recusa continuar."""
    meta: dict[str, Any] = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    now = datetime.now(UTC).isoformat(timespec="seconds")
    if meta and (meta.get("judge_prompt_hash") != prompt_hash() or meta.get("dataset_hash") != dataset_sha):
        raise PreflightError(
            f"o juiz '{judge}' já tem julgamentos deste run com outro prompt do juiz ou outro dataset "
            "(hash diferente): use outro --run-id"
        )
    meta.setdefault("created_at", now)
    meta.setdefault("started_at", now)
    meta.update(
        judge=judge, model=model, ollama_version=ollama_version, run_id=run_id, scope=SCOPE,
        judge_prompt_version=JUDGE_PROMPT_VERSION, judge_prompt_hash=prompt_hash(), dataset_hash=dataset_sha,
        config_hash=config_sha, temperature=0, max_tokens=JUDGE_MAX_TOKENS,
        resumed=resumed or meta.get("resumed", False), **git_info(),
    )
    if finished:
        meta["finished_at"] = now
        meta["summary"] = summary
    write_json_atomic(path, meta)


@dataclass
class Options:
    run_id: str
    judge: str
    resume: bool = False
    limit: int | None = None
    config_path: Path = EVAL_CONFIG
    dataset_dir: Path = PROMPTS_DIR
    results_root: Path = RESULTS_ROOT


def load_inputs(opts: Options) -> tuple[Path, dict[str, EvalPrompt], dict[str, list[dict[str, Any]]]]:
    run_dir = opts.results_root / opts.run_id
    try:
        results = load_run(run_dir)
        dataset = load_dataset(opts.dataset_dir)
    except ScoreError as e:
        raise PreflightError(f"{e}. Rode antes o evalkit.run para os dois provedores.") from e
    except DatasetError as e:
        raise PreflightError("dataset inválido (rode python -m evalkit.validate_dataset)") from e
    meta_path = run_dir / "meta.json"
    if meta_path.exists():
        run_sha = json.loads(meta_path.read_text(encoding="utf-8")).get("dataset_hash")
        if run_sha and run_sha != dataset_hash(opts.dataset_dir):
            raise PreflightError("o dataset mudou desde a coleta deste run (hash diferente): não dá para julgar")
    return run_dir, dataset.by_id(), results


def select_ids(prompts: dict[str, EvalPrompt], limit: int | None) -> list[str]:
    return [p.id for p in select_prompts(list(prompts.values()), limit)]


async def run_session(gateway_url: str, items: list[Item], opts: Options, out_path: Path,
                      out: Callable[[str], None]) -> list[dict[str, Any]]:
    async with make_gateway_client(gateway_url) as gw:
        async def chat(messages: list[dict[str, str]]) -> dict[str, Any]:
            return await post_chat(gw, opts.judge, messages)

        # aquecimento descartado: a primeira chamada carrega o modelo na VRAM
        await post_chat(gw, opts.judge, [{"role": "user", "content": "Diga olá em uma palavra."}], max_tokens=8)
        return await collect(chat, items, opts.judge, out_path, resume=opts.resume, out=out)


def execute(opts: Options, out: Callable[[str], None] = print) -> dict[str, int]:
    """Pré-checagens, pilha de processos, coleta e meta. Devolve o resumo."""
    if not RUN_ID_RE.match(opts.run_id):
        raise PreflightError("--run-id inválido: use letras, números, '.', '_' ou '-' (até 64 caracteres)")
    guard_environment()
    try:
        cfg = load_config(opts.config_path)
    except (OSError, ValidationError, yaml.YAMLError) as e:
        raise PreflightError(f"config de avaliação inválido ({opts.config_path.name}): {e}") from e
    check_no_anthropic(cfg)
    check_provider(cfg, opts.judge)
    run_dir, prompts, results = load_inputs(opts)
    items = plan_items(prompts, results, select_ids(prompts, opts.limit))

    async def preflight() -> str:
        async with make_ollama_client() as client:
            return await check_ollama(client, cfg, opts.judge)

    ollama_version = asyncio.run(preflight())
    jdir = judge_dir(run_dir)
    out_path = jdir / f"{opts.judge}.jsonl"
    if out_path.exists() and out_path.stat().st_size and not opts.resume:
        raise PreflightError(f"{out_path.name} já existe em {jdir}: use --resume para continuar ou outro --run-id")
    workdir = jdir / "gateway" / opts.judge
    workdir.mkdir(parents=True, exist_ok=True)
    model = cfg.provider(opts.judge).model
    meta_path = jdir / f"{opts.judge}.meta.json"
    meta_args: dict[str, Any] = {
        "judge": opts.judge, "model": model, "ollama_version": ollama_version, "run_id": opts.run_id,
        "dataset_sha": dataset_hash(opts.dataset_dir), "config_sha": file_hash(opts.config_path),
        "resumed": opts.resume,
    }
    update_meta(meta_path, finished=False, **meta_args)

    gateway_port, mock_port = free_ports()
    raw = build_runtime_config(opts.config_path, provider=opts.judge, gateway_port=gateway_port,
                               mock_port=mock_port, workdir=workdir)
    config_file = workdir / "config.runtime.yaml"
    config_file.write_text(yaml.safe_dump(raw, allow_unicode=True), encoding="utf-8")
    out(f"Subindo o gateway (127.0.0.1:{gateway_port}); a chain é só o juiz {opts.judge} ({model}). Sem Anthropic.")
    out(f"{len(items)} julgamento(s) planejado(s). O aquecimento carrega o modelo e pode demorar.")
    with start_stack(config_file, workdir, gateway_port, mock_port) as stack:
        records = asyncio.run(run_session(stack.gateway_url, items, opts, out_path, out))
    summary = summarize(records)
    update_meta(meta_path, finished=True, summary=summary, **meta_args)
    out(f"{opts.judge} ({model}): {summary['total']} julgamentos, {summary['ok']} ok, "
        f"{summary['judge_errors']} judge_error, {summary['retried']} com nova tentativa")
    return summary


def parse_args(argv: list[str] | None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Coleta julgamentos de um juiz local (sem Anthropic).")
    p.add_argument("--run-id", required=True, help="run já coletado e pontuado (pasta em eval/results/)")
    p.add_argument("--judge", required=True, help="ollama_qwen ou ollama_llama (um por vez)")
    p.add_argument("--resume", action="store_true", help="pula os julgamentos já gravados")
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
        execute(Options(run_id=args.run_id, judge=args.judge, resume=args.resume, limit=args.limit))
        return 0
    except (PreflightError, StackError) as e:
        print(f"erro: {e}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("\nInterrompido; processos encerrados. Continue com --resume.", file=sys.stderr)
        return 130


if __name__ == "__main__":
    sys.exit(main())
