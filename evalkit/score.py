"""CLI de pontuação e estatística da avaliação (Fase 3). Sem rede, sem Ollama, sem Anthropic.

    python -m evalkit.score --run-id <id> [--seed N] [--no-embedding | --allow-download] [--review-sample]

Lê `eval/results/<id>/*.jsonl` (um arquivo por provedor, gerado por `evalkit.run`) e grava na mesma pasta
`scores.jsonl` (um registro por provedor x prompt) e `summary.json` (ignorados pelo git).

Qualidade: média do score e taxa de aprovação (intervalo de Wilson 95%) só sobre respostas AVALIÁVEIS;
erro/timeout/resposta vazia ficam à parte em `errors`. Latência (p50/p95, ponta a ponta no cliente) e
tokens/s (`completion_tokens` / tempo da requisição, que inclui o gateway e o processamento do prompt)
só sobre respostas com sucesso. Comparação pareada por prompt: diferença média com IC bootstrap
(semente fixa) e McNemar exato; com IC que inclui zero o texto diz "indicativo, não conclusivo".
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import random
import sys
from collections import Counter
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from evalkit.embedding import DEFAULT_MODEL, Embedder, load_embedder
from evalkit.schema import TASK_TYPES, EvalPrompt
from evalkit.scorers import score_record
from evalkit.stats import bootstrap_mean_ci, mcnemar_exact, percentile, wilson_interval
from evalkit.validate_dataset import PROMPTS_DIR, DatasetError, dataset_hash, load_dataset
from gateway.config import PROJECT_ROOT

RESULTS_ROOT = PROJECT_ROOT / "eval" / "results"
DEFAULT_SEED = 20260
NON_RESULT_FILES = {"scores.jsonl"}
PREFERRED_PAIR = ("ollama_qwen", "ollama_llama")
INCONCLUSIVE = "indicativo, não conclusivo"
MIN_PAIRS = 5  # abaixo disso o bootstrap é degenerado: nunca se declara diferença


class ScoreError(Exception):
    """Pré-requisito ausente: mensagem clara, sem traceback."""


# --------------------------------------------------------------------------- leitura e pontuação
def load_run(run_dir: Path) -> dict[str, list[dict[str, Any]]]:
    """Resultados por provedor (`<provider>.jsonl` no topo da pasta). Linhas inválidas são ignoradas."""
    if not run_dir.is_dir():
        raise ScoreError(f"run não encontrado: {run_dir}")
    out: dict[str, list[dict[str, Any]]] = {}
    for path in sorted(run_dir.glob("*.jsonl")):
        if path.name in NON_RESULT_FILES:
            continue
        records = []
        for line in path.read_text(encoding="utf-8").splitlines():
            with contextlib.suppress(ValueError):
                rec = json.loads(line)
                if isinstance(rec, dict) and "prompt_id" in rec:
                    records.append(rec)
        out[path.stem] = records
    if not out:
        raise ScoreError(f"nenhum <provedor>.jsonl em {run_dir}")
    return out


def tokens_per_s(rec: dict[str, Any]) -> float | None:
    usage = rec.get("usage")
    tokens = usage.get("completion_tokens") if isinstance(usage, dict) else None
    latency = rec.get("latency_ms")
    if not isinstance(tokens, int | float) or not isinstance(latency, int | float) or tokens <= 0 or latency <= 0:
        return None
    return tokens / (latency / 1000)


def score_run(
    results: dict[str, list[dict[str, Any]]], prompts: dict[str, EvalPrompt], embedder: Embedder | None
) -> list[dict[str, Any]]:
    rows = []
    for provider, records in sorted(results.items()):
        # ordem explícita (provedor, prompt_id): a saída não depende da ordem das linhas nem dos arquivos
        for rec in sorted(records, key=lambda r: str(r.get("prompt_id"))):
            prompt = prompts.get(rec.get("prompt_id"))
            if prompt is None:
                rows.append({
                    "prompt_id": rec.get("prompt_id"), "provider": provider, "task_type": rec.get("task_type"),
                    "score": 0.0, "passed": False, "errored": True, "error_kind": "unknown_prompt",
                    "details": {"error": "prompt_id fora do dataset"},
                })
                continue
            res = score_record(prompt, rec, embedder)
            ok = not res["errored"]
            usage = rec.get("usage") if isinstance(rec.get("usage"), dict) else {}
            rows.append({
                "prompt_id": prompt.id, "provider": provider, "task_type": prompt.task_type, **res,
                "latency_ms": rec.get("latency_ms") if ok else None,
                "tokens_per_s": tokens_per_s(rec) if ok else None,
                "completion_tokens": usage.get("completion_tokens") if ok else None,
            })
    return rows


# --------------------------------------------------------------------------- agregação
def _round(x: float | None, nd: int = 4) -> float | None:
    return None if x is None else round(x, nd)


def group_stats(rows: list[dict[str, Any]]) -> dict[str, Any]:
    scored = [r for r in rows if not r["errored"]]
    passed = sum(r["passed"] for r in scored)
    wil = wilson_interval(passed, len(scored))
    lat = [r["latency_ms"] for r in scored if r.get("latency_ms") is not None]
    tps = [r["tokens_per_s"] for r in scored if r.get("tokens_per_s") is not None]
    return {
        "n": len(rows),
        "scored": len(scored),
        "errors": len(rows) - len(scored),
        "error_kinds": dict(Counter(r["error_kind"] for r in rows if r["errored"])),
        "mean_score": _round(sum(r["score"] for r in scored) / len(scored)) if scored else None,
        "passed": passed,
        "pass_rate": _round(passed / len(scored)) if scored else None,
        "pass_rate_ci95": [_round(wil[0]), _round(wil[1])] if wil else None,
        "latency_p50_ms": _round(percentile(lat, 50), 1),
        "latency_p95_ms": _round(percentile(lat, 95), 1),
        "tokens_per_s_p50": _round(percentile(tps, 50), 1),
        "tokens_per_s_p95": _round(percentile(tps, 95), 1),
    }


def pick_pair(providers: list[str]) -> tuple[str, str] | None:
    if all(p in providers for p in PREFERRED_PAIR):
        return PREFERRED_PAIR
    ordered = sorted(providers)
    return (ordered[0], ordered[1]) if len(ordered) >= 2 else None


def verdict(block: dict[str, Any], names: tuple[str, str] = ("A", "B")) -> str:
    ci = block["diff_ci95"]
    if ci is None:
        return "sem pares avaliáveis"
    if block["pairs"] < MIN_PAIRS:
        return f"{INCONCLUSIVE} (só {block['pairs']} pares); nenhum vencedor declarado"
    if ci[0] <= 0 <= ci[1]:
        return f"{INCONCLUSIVE} (o IC95% da diferença inclui zero); nenhum vencedor declarado"
    p = block["mcnemar"]["p_exact"]
    if p >= 0.05:
        return f"{INCONCLUSIVE} (o IC95% exclui zero, mas o McNemar exato não confirma: p={p}); nenhum vencedor"
    side = names[0] if block["mean_diff_a_minus_b"] > 0 else names[1]
    return f"diferença detectada a favor de {side} (o IC95% exclui zero); amostra pequena, leia com cautela"


def paired_block(
    a_rows: dict[str, dict[str, Any]], b_rows: dict[str, dict[str, Any]], seed: int, names: tuple[str, str] = ("A", "B")
) -> dict[str, Any]:
    """Comparação por prompt entre A e B. Pares com erro em qualquer lado ficam de fora (`excluded`)."""
    ids = sorted(set(a_rows) & set(b_rows))
    ok = [i for i in ids if not a_rows[i]["errored"] and not b_rows[i]["errored"]]
    diffs = [a_rows[i]["score"] - b_rows[i]["score"] for i in ok]
    only_a = sum(a_rows[i]["passed"] and not b_rows[i]["passed"] for i in ok)
    only_b = sum(b_rows[i]["passed"] and not a_rows[i]["passed"] for i in ok)
    ci = bootstrap_mean_ci(diffs, seed=seed)
    block: dict[str, Any] = {
        "pairs": len(ok),
        "excluded_for_errors": [i for i in ids if i not in ok],
        "mean_diff_a_minus_b": _round(ci[0]) if ci else None,
        "diff_ci95": [_round(ci[1]), _round(ci[2])] if ci else None,
        "mcnemar": {"only_a_passes": only_a, "only_b_passes": only_b, "p_exact": _round(mcnemar_exact(only_a, only_b))},
    }
    block["verdict"] = verdict(block, names)
    return block


def both_fail_lists(
    a_rows: dict[str, dict[str, Any]], b_rows: dict[str, dict[str, Any]], names: tuple[str, str]
) -> tuple[list[str], list[dict[str, str]]]:
    ids = sorted(i for i in set(a_rows) & set(b_rows) if not a_rows[i]["errored"] and not b_rows[i]["errored"])
    both = [i for i in ids if not a_rows[i]["passed"] and not b_rows[i]["passed"]]
    one = [
        {"prompt_id": i, "fails": names[0] if not a_rows[i]["passed"] else names[1]}
        for i in ids
        if a_rows[i]["passed"] != b_rows[i]["passed"]
    ]
    return both, one


def build_summary(
    rows: list[dict[str, Any]], *, run_id: str, seed: int, embedding: str, meta: dict[str, Any] | None,
    dataset_sha: str | None,
) -> dict[str, Any]:
    by_provider: dict[str, list[dict[str, Any]]] = {}
    for r in rows:
        by_provider.setdefault(r["provider"], []).append(r)
    providers = {}
    for p, prows in sorted(by_provider.items()):
        providers[p] = {
            "overall": group_stats(prows),
            "by_task_type": {
                t: group_stats([r for r in prows if r["task_type"] == t])
                for t in TASK_TYPES
                if any(r["task_type"] == t for r in prows)
            },
        }
    summary: dict[str, Any] = {
        "run_id": run_id, "seed": seed, "embedding": embedding,
        "scope": (meta or {}).get("scope"), "dataset_hash_run": (meta or {}).get("dataset_hash"),
        "dataset_hash_now": dataset_sha, "providers": providers, "paired": None,
    }
    pair = pick_pair(list(providers))
    if pair:
        a = {r["prompt_id"]: r for r in by_provider[pair[0]]}
        b = {r["prompt_id"]: r for r in by_provider[pair[1]]}
        per_type = {}
        for t in TASK_TYPES:
            ta = {i: r for i, r in a.items() if r["task_type"] == t}
            tb = {i: r for i, r in b.items() if r["task_type"] == t}
            if ta and tb:
                per_type[t] = paired_block(ta, tb, seed, pair)
        both, one = both_fail_lists(a, b, pair)
        summary["paired"] = {
            "a": pair[0], "b": pair[1], "overall": paired_block(a, b, seed, pair), "by_task_type": per_type,
            "both_fail": both, "only_one_fails": one,
        }
    return summary


# --------------------------------------------------------------------------- amostra para revisão humana
def pick_review_sample(
    rows: list[dict[str, Any]], per_category: int = 2, seed: int = DEFAULT_SEED
) -> list[dict[str, Any]]:
    """`per_category` respostas por categoria, escolhidas por semente fixa (prompt e provedor)."""
    rng = random.Random(seed)
    sample = []
    for t in TASK_TYPES:
        pool = sorted(
            [r for r in rows if r["task_type"] == t and not r["errored"]], key=lambda r: (r["prompt_id"], r["provider"])
        )
        sample.extend(rng.sample(pool, min(per_category, len(pool))))
    return sample


# --------------------------------------------------------------------------- saída
def _pct(x: float | None) -> str:
    return "-" if x is None else f"{x * 100:.0f}%"


def _num(x: float | None, nd: int = 0) -> str:
    return "-" if x is None else f"{x:.{nd}f}"


def format_summary(summary: dict[str, Any]) -> str:
    lines = [f"run {summary['run_id']} | embedding: {summary['embedding']} | semente {summary['seed']}", ""]
    header = (f"{'provedor':<13} {'categoria':<15} {'n':>3} {'erros':>5} {'score':>6} {'aprov.':>7} "
              f"{'IC95 aprov.':>12} {'p50 ms':>7} {'p95 ms':>7} {'tok/s p50':>9}")
    lines += [header, "-" * len(header)]
    for p, data in summary["providers"].items():
        for label, st in [("(todas)", data["overall"]), *data["by_task_type"].items()]:
            ci = st["pass_rate_ci95"]
            ci_text = f"{ci[0] * 100:.0f}-{ci[1] * 100:.0f}%" if ci else "-"
            lines.append(
                f"{p:<13} {label:<15} {st['n']:>3} {st['errors']:>5} {_num(st['mean_score'], 2):>6} "
                f"{_pct(st['pass_rate']):>7} {ci_text:>12} {_num(st['latency_p50_ms']):>7} "
                f"{_num(st['latency_p95_ms']):>7} {_num(st['tokens_per_s_p50'], 1):>9}"
            )
    pr = summary.get("paired")
    if pr:
        lines += ["", f"Comparação pareada ({pr['a']} - {pr['b']}), por prompt:"]
        for label, blk in [("(todas)", pr["overall"]), *pr["by_task_type"].items()]:
            m = blk["mcnemar"]
            lines.append(
                f"  {label:<15} pares={blk['pairs']:>2} dif={_num(blk['mean_diff_a_minus_b'], 2):>6} "
                f"IC95={blk['diff_ci95']} McNemar a={m['only_a_passes']} b={m['only_b_passes']} "
                f"p={m['p_exact']} -> {blk['verdict']}"
            )
        lines.append(f"  os dois reprovam ({len(pr['both_fail'])}): {', '.join(pr['both_fail']) or '-'}")
        one = [f"{x['prompt_id']} ({x['fails']})" for x in pr["only_one_fails"]]
        lines.append(f"  só um reprova ({len(one)}): {', '.join(one) or '-'}")
    if summary.get("scope"):
        lines += ["", f"Escopo: {summary['scope']}"]
    return "\n".join(lines)


def format_review(
    sample: list[dict[str, Any]], prompts: dict[str, EvalPrompt], results: dict[str, list[dict[str, Any]]]
) -> str:
    by_key = {(p, r["prompt_id"]): r for p, recs in results.items() for r in recs}
    blocks = []
    for i, row in enumerate(sample, 1):
        prompt = prompts[row["prompt_id"]]
        rec = by_key[(row["provider"], row["prompt_id"])]
        kind = prompt.check.type if prompt.check else "summarization"
        blocks.append("\n".join([
            f"### {i}. {row['prompt_id']} | {row['provider']} | {kind}",
            "PROMPT:", prompt.messages[-1].content, "RESPOSTA:", str(rec.get("response")),
            f"SCORE: {row['score']} | passed: {row['passed']}",
            "DETAILS: " + json.dumps(row["details"], ensure_ascii=False),
        ]))
    return "\n\n".join(blocks)


def parse_args(argv: list[str] | None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Pontua e resume um run de avaliação (sem rede, sem Anthropic).")
    p.add_argument("--run-id", required=True)
    p.add_argument("--seed", type=int, default=DEFAULT_SEED, help="semente do bootstrap e da amostra de revisão")
    emb = p.add_mutually_exclusive_group()
    emb.add_argument("--no-embedding", action="store_true", help="não tenta carregar o modelo de embedding")
    emb.add_argument("--allow-download", action="store_true", help="PERMITE baixar o modelo (padrão: só o cache)")
    p.add_argument("--embedding-model", default=DEFAULT_MODEL)
    p.add_argument("--review-sample", action="store_true", help="imprime 2 respostas por categoria para revisão humana")
    p.add_argument("--results-root", type=Path, default=RESULTS_ROOT, help=argparse.SUPPRESS)
    return p.parse_args(argv)


def record_scoring_meta(run_dir: Path, meta: dict[str, Any] | None, summary: dict[str, Any]) -> None:
    """Registra no `meta.json` do run COMO a pontuação foi feita (modelo, revision, device), para reproduzir."""
    if meta is None:
        return
    meta["scoring"] = {
        "scored_at": datetime.now(UTC).isoformat(timespec="seconds"), "seed": summary["seed"],
        "embedding": summary["embedding"], "embedding_info": summary["embedding_info"],
    }
    path = run_dir / "meta.json"
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(meta, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def execute(args: argparse.Namespace, out: Callable[[str], None] = print) -> dict[str, Any]:
    run_dir = args.results_root / args.run_id
    results = load_run(run_dir)
    try:
        dataset = load_dataset(PROMPTS_DIR)
    except DatasetError as e:
        raise ScoreError("dataset inválido (rode python -m evalkit.validate_dataset)") from e
    prompts = dataset.by_id()
    embedder: Embedder | None = None
    embedding = "disabled"
    if not args.no_embedding:
        embedder, embedding = load_embedder(args.embedding_model, allow_download=args.allow_download)
    rows = score_run(results, prompts, embedder)
    meta_path = run_dir / "meta.json"
    meta = json.loads(meta_path.read_text(encoding="utf-8")) if meta_path.exists() else None
    info = embedder.info() if embedder is not None and hasattr(embedder, "info") else None
    summary = build_summary(
        rows, run_id=args.run_id, seed=args.seed, embedding=embedding, meta=meta, dataset_sha=dataset_hash(PROMPTS_DIR)
    )
    summary["embedding_info"] = info
    scores_text = "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows)
    (run_dir / "scores.jsonl").write_text(scores_text, encoding="utf-8")
    (run_dir / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    record_scoring_meta(run_dir, meta, summary)
    out(format_summary(summary))
    if summary["dataset_hash_run"] and summary["dataset_hash_run"] != summary["dataset_hash_now"]:
        out("\naviso: o dataset mudou desde a coleta (hash diferente do meta.json); registre uma nova versão.")
    if args.review_sample:
        out("\n" + format_review(pick_review_sample(rows, seed=args.seed), prompts, results))
    return summary


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    for stream in (sys.stdout, sys.stderr):
        with contextlib.suppress(AttributeError, ValueError):
            stream.reconfigure(encoding="utf-8", errors="replace")
    try:
        execute(args)
        return 0
    except ScoreError as e:
        print(f"erro: {e}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
