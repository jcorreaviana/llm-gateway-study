"""Estatística e CLI de pontuação (Fase 3), com valores conhecidos. Sem rede e sem Ollama."""

import json
import math
import random
import re
import subprocess
import sys
from pathlib import Path

import pytest

from evalkit import score as sc
from evalkit.embedding import load_embedder
from evalkit.stats import bootstrap_mean_ci, mcnemar_exact, percentile, wilson_interval
from evalkit.validate_dataset import load_dataset

REPO_ROOT = Path(__file__).resolve().parent.parent


# --------------------------------------------------------------------------- Wilson
@pytest.mark.parametrize(
    ("k", "n", "lo", "hi"),
    [(8, 10, 0.4902, 0.9433), (5, 10, 0.2366, 0.7634), (0, 10, 0.0, 0.2775), (10, 10, 0.7225, 1.0),
     (1, 8, 0.0223, 0.4710), (50, 100, 0.4038, 0.5962)],
)
def test_wilson_known_values(k, n, lo, hi):
    got = wilson_interval(k, n)
    assert got[0] == pytest.approx(lo, abs=1e-3) and got[1] == pytest.approx(hi, abs=1e-3)


def test_wilson_edges():
    assert wilson_interval(0, 0) is None
    with pytest.raises(ValueError):
        wilson_interval(5, 4)
    lo, hi = wilson_interval(3, 7)
    assert 0 <= lo < 3 / 7 < hi <= 1


# --------------------------------------------------------------------------- McNemar exato
@pytest.mark.parametrize(
    ("a", "b", "p"),
    [(0, 0, 1.0), (3, 3, 1.0), (0, 5, 0.0625), (5, 0, 0.0625), (1, 5, 14 / 64), (0, 10, 2 / 1024), (2, 8, 0.109375),
     (1, 0, 1.0)],
)
def test_mcnemar_exact_known_values(a, b, p):
    assert mcnemar_exact(a, b) == pytest.approx(p)


def test_mcnemar_is_symmetric_and_capped_at_one():
    assert mcnemar_exact(2, 7) == mcnemar_exact(7, 2)
    assert mcnemar_exact(4, 5) <= 1.0


# --------------------------------------------------------------------------- percentis
def test_percentile_linear_interpolation():
    assert percentile([1, 2, 3, 4], 50) == 2.5
    assert percentile([1, 2, 3, 4], 95) == pytest.approx(3.85)
    assert percentile([10], 95) == 10
    assert percentile([], 50) is None
    assert percentile([5, 1, 3], 0) == 1 and percentile([5, 1, 3], 100) == 5


# --------------------------------------------------------------------------- bootstrap
def test_bootstrap_is_reproducible_and_does_not_touch_the_global_random_state():
    data = [1, 0, 1, 1, 0, -1, 1, 0]
    random.seed(99)
    expected_next = random.random()
    random.seed(99)
    assert bootstrap_mean_ci(data, seed=7) == bootstrap_mean_ci(data, seed=7)
    assert random.random() == expected_next
    assert bootstrap_mean_ci(data, seed=7, n_boot=50) != bootstrap_mean_ci(data, seed=8, n_boot=50)


def test_bootstrap_golden_values_for_seed_20260():
    mean, lo, hi = bootstrap_mean_ci([1, 0, 1, 1, 0, -1, 1, 0], seed=20260, n_boot=2000)
    assert mean == pytest.approx(0.375)
    assert (round(lo, 3), round(hi, 3)) == GOLDEN_BOOT
    # conferência independente: erro-padrão ~0,26 => IC grosso modo 0,375 +- 0,5
    assert lo == pytest.approx(0.375 - 0.5, abs=0.15) and hi == pytest.approx(0.375 + 0.5, abs=0.15)


def test_bootstrap_degenerate_and_empty():
    assert bootstrap_mean_ci([0.5] * 8) == (0.5, 0.5, 0.5)
    assert bootstrap_mean_ci([]) is None
    mean, lo, hi = bootstrap_mean_ci([1, 1, 1, 1, 1, 1, 1, 1, 0])
    assert lo <= mean <= hi and not math.isnan(lo)


def test_bootstrap_interval_covers_zero_for_balanced_diffs_and_not_for_one_sided():
    _, lo, hi = bootstrap_mean_ci([1, -1, 1, -1, 1, -1, 0, 0])
    assert lo < 0 < hi
    _, lo, hi = bootstrap_mean_ci([1, 1, 1, 1, 1, 0, 1, 1])
    assert lo > 0


GOLDEN_BOOT = (-0.125, 0.75)  # pinado para a semente 20260 (n_boot=2000): muda só se o algoritmo mudar


# --------------------------------------------------------------------------- agregação e relatório
def row(pid, provider, task, score=1.0, passed=True, errored=False, kind=None, lat=100.0, tokens=10):
    ok = not errored
    return {"prompt_id": pid, "provider": provider, "task_type": task, "score": 0.0 if errored else score,
            "passed": False if errored else passed, "errored": errored, "error_kind": kind, "details": {},
            "latency_ms": lat if ok else None, "tokens_per_s": tokens / (lat / 1000) if ok else None}


def test_group_stats_counts_errors_apart_from_quality():
    rows = [row("a-01", "p", "t", 1.0, True), row("a-02", "p", "t", 0.0, False),
            row("a-03", "p", "t", errored=True, kind="http_error"),
            row("a-04", "p", "t", errored=True, kind="empty_response")]
    st = sc.group_stats(rows)
    assert (st["n"], st["scored"], st["errors"]) == (4, 2, 2)
    assert st["error_kinds"] == {"http_error": 1, "empty_response": 1}
    assert st["mean_score"] == 0.5 and st["pass_rate"] == 0.5  # os erros não puxam a média para baixo
    assert st["latency_p50_ms"] == 100.0 and st["tokens_per_s_p50"] == pytest.approx(100.0)
    assert st["pass_rate_ci95"][0] < 0.5 < st["pass_rate_ci95"][1]


def test_group_stats_without_scored_rows():
    st = sc.group_stats([row("a-01", "p", "t", errored=True, kind="http_error")])
    assert st["mean_score"] is None and st["pass_rate"] is None and st["pass_rate_ci95"] is None


def _pair_rows(a_pass, b_pass, task="extraction", provider_a="ollama_qwen", provider_b="ollama_llama"):
    a = {f"{task}-{i:02d}": row(f"{task}-{i:02d}", provider_a, task, float(p), bool(p)) for i, p in enumerate(a_pass, 1)}
    b = {f"{task}-{i:02d}": row(f"{task}-{i:02d}", provider_b, task, float(p), bool(p)) for i, p in enumerate(b_pass, 1)}
    return a, b


def test_paired_block_identical_models_is_inconclusive():
    a, b = _pair_rows([1, 0, 1, 1, 0, 1, 1, 0], [1, 0, 1, 1, 0, 1, 1, 0])
    blk = sc.paired_block(a, b, 1)
    assert blk["mean_diff_a_minus_b"] == 0 and blk["mcnemar"]["p_exact"] == 1.0
    assert sc.INCONCLUSIVE in blk["verdict"] and "nenhum vencedor" in blk["verdict"]


def test_paired_block_says_inconclusive_when_ci_includes_zero_even_if_means_differ():
    a, b = _pair_rows([1, 1, 1, 0, 1, 0, 1, 1], [1, 0, 1, 1, 0, 1, 0, 1])
    blk = sc.paired_block(a, b, 1)
    assert blk["diff_ci95"][0] <= 0 <= blk["diff_ci95"][1]
    assert sc.INCONCLUSIVE in blk["verdict"]


def test_paired_block_declares_a_difference_only_when_ci_excludes_zero():
    a, b = _pair_rows([1] * 8, [0, 0, 0, 0, 0, 0, 0, 1])
    blk = sc.paired_block(a, b, 1, ("ollama_qwen", "ollama_llama"))
    assert blk["diff_ci95"][0] > 0 and "a favor de ollama_qwen" in blk["verdict"]
    assert blk["mcnemar"] == {"only_a_passes": 7, "only_b_passes": 0, "p_exact": 0.0156}


def test_paired_block_refuses_to_declare_with_very_few_pairs():
    a, b = _pair_rows([1, 1], [0, 0])
    assert sc.INCONCLUSIVE in sc.paired_block(a, b, 1)["verdict"]


def test_paired_block_excludes_pairs_with_errors():
    a, b = _pair_rows([1, 1, 1, 1, 1, 1], [1, 1, 1, 1, 1, 1])
    b["extraction-02"] = row("extraction-02", "ollama_llama", "extraction", errored=True, kind="http_error")
    blk = sc.paired_block(a, b, 1)
    assert blk["pairs"] == 5 and blk["excluded_for_errors"] == ["extraction-02"]


def test_both_fail_and_only_one_fails_lists():
    a, b = _pair_rows([1, 0, 0, 1], [1, 0, 1, 0])
    both, one = sc.both_fail_lists(a, b, ("ollama_qwen", "ollama_llama"))
    assert both == ["extraction-02"]
    assert one == [{"prompt_id": "extraction-03", "fails": "ollama_qwen"},
                   {"prompt_id": "extraction-04", "fails": "ollama_llama"}]


def test_pick_pair():
    assert sc.pick_pair(["ollama_llama", "ollama_qwen"]) == ("ollama_qwen", "ollama_llama")
    assert sc.pick_pair(["b", "a"]) == ("a", "b")
    assert sc.pick_pair(["a"]) is None


def test_tokens_per_s():
    assert sc.tokens_per_s({"usage": {"completion_tokens": 50}, "latency_ms": 500}) == 100.0
    assert sc.tokens_per_s({"usage": None, "latency_ms": 500}) is None
    assert sc.tokens_per_s({"usage": {"completion_tokens": 0}, "latency_ms": 500}) is None


# --------------------------------------------------------------------------- CLI de ponta a ponta (sem rede)
def synthetic_results(tmp_path):
    prompts = load_dataset().prompts
    run = tmp_path / "r1"
    run.mkdir()
    answers = {
        "numeric": lambda c: str(c.spec["value"]),
        "label_in": lambda c: c.spec["expected"],
        "exact": lambda c: c.spec["value"],
    }
    lines = {"ollama_qwen": [], "ollama_llama": []}
    for i, p in enumerate(prompts):
        for provider in lines:
            base = {"prompt_id": p.id, "task_type": p.task_type, "provider": provider, "status": 200,
                    "latency_ms": 100.0 + i, "usage": {"completion_tokens": 20}, "finish_reason": "stop"}
            if p.check and p.check.type in answers and not (provider == "ollama_llama" and i % 5 == 0):
                base["response"] = answers[p.check.type](p.check)
            elif i % 11 == 0 and provider == "ollama_llama":
                base.update(status=0, response=None, error="ReadTimeout: x", latency_ms=1000.0)
            else:
                base["response"] = "resposta qualquer"
            lines[provider].append(base)
    for provider, recs in lines.items():
        (run / f"{provider}.jsonl").write_text("".join(json.dumps(r) + "\n" for r in recs), encoding="utf-8")
    return tmp_path


def test_cli_writes_scores_and_summary_and_reports_without_a_winner(tmp_path, capsys):
    root = synthetic_results(tmp_path)
    assert sc.main(["--run-id", "r1", "--results-root", str(root), "--no-embedding", "--review-sample"]) == 0
    out = capsys.readouterr().out
    run = root / "r1"
    scores = [json.loads(line) for line in (run / "scores.jsonl").read_text(encoding="utf-8").splitlines()]
    summary = json.loads((run / "summary.json").read_text(encoding="utf-8"))
    assert len(scores) == 96 and summary["embedding"] == "disabled"
    assert set(summary["providers"]) == {"ollama_qwen", "ollama_llama"}
    assert summary["providers"]["ollama_llama"]["overall"]["errors"] >= 1
    assert summary["providers"]["ollama_qwen"]["overall"]["errors"] == 0
    assert summary["paired"]["a"] == "ollama_qwen" and summary["paired"]["both_fail"]
    assert "tok/s" in out and "### 12." in out and "### 13." not in out  # 12 respostas na amostra
    assert all(r["errored"] is False for r in scores if r["provider"] == "ollama_qwen")


def test_cli_is_deterministic_for_a_fixed_seed(tmp_path):
    root = synthetic_results(tmp_path)
    args = ["--run-id", "r1", "--results-root", str(root), "--no-embedding", "--seed", "5"]
    sc.main(args)
    first = (root / "r1" / "summary.json").read_text(encoding="utf-8")
    sc.main(args)
    assert (root / "r1" / "summary.json").read_text(encoding="utf-8") == first


def test_review_sample_is_two_per_category_and_fixed_by_seed(tmp_path):
    root = synthetic_results(tmp_path)
    results = sc.load_run(root / "r1")
    prompts = load_dataset().by_id()
    rows = sc.score_run(results, prompts, None)
    s1, s2 = sc.pick_review_sample(rows, seed=3), sc.pick_review_sample(rows, seed=3)
    assert [(r["prompt_id"], r["provider"]) for r in s1] == [(r["prompt_id"], r["provider"]) for r in s2]
    assert len(s1) == 12
    assert sorted({r["task_type"] for r in s1}) == sorted(set(sc.TASK_TYPES))
    assert all(sum(1 for r in s1 if r["task_type"] == t) == 2 for t in sc.TASK_TYPES)


def test_cli_missing_run_is_a_clean_error(tmp_path, capsys):
    assert sc.main(["--run-id", "nao-existe", "--results-root", str(tmp_path)]) == 2
    assert "run não encontrado" in capsys.readouterr().err


def test_unknown_prompt_id_is_counted_as_error_not_crash(tmp_path):
    run = tmp_path / "r2"
    run.mkdir()
    (run / "ollama_qwen.jsonl").write_text(json.dumps({"prompt_id": "ghost-01", "status": 200, "response": "x"}) + "\n",
                                           encoding="utf-8")
    rows = sc.score_run(sc.load_run(run), load_dataset().by_id(), None)
    assert rows[0]["errored"] is True and rows[0]["error_kind"] == "unknown_prompt"


def test_embedding_unavailable_when_dependency_or_model_is_missing(offline_hf):
    emb, status = load_embedder("modelo-que-nao-existe/xyz")  # só cache local: nunca baixa
    assert emb is None and status.startswith("unavailable")


def test_scoring_code_never_imports_a_network_client_or_anthropic():
    for name in ("scorers", "embedding", "stats", "score"):
        src = (REPO_ROOT / "evalkit" / f"{name}.py").read_text(encoding="utf-8")
        assert not re.search(r"^\s*(import|from)\s+(httpx|requests|urllib|socket|anthropic)\b", src, re.M), name


def test_sentence_transformers_is_not_a_default_dependency():
    for name in ("requirements.txt", ".github/workflows/ci.yml"):
        assert "sentence-transformers" not in (REPO_ROOT / name).read_text(encoding="utf-8")
    assert "sentence-transformers" in (REPO_ROOT / "requirements-eval.txt").read_text(encoding="utf-8")
    proc = subprocess.run([sys.executable, "-c", "import sys, evalkit.score; print('sentence_transformers' in sys.modules)"],
                          capture_output=True, text=True, cwd=REPO_ROOT, check=True)
    assert proc.stdout.strip() == "False"  # importar o CLI não carrega o modelo nem a dependência


def test_verdict_is_inconclusive_when_ci_excludes_zero_but_mcnemar_does_not_confirm():
    a, b = _pair_rows([1] * 8, [0, 0, 0, 1, 1, 1, 1, 1])
    for pid in list(b)[3:]:  # empates na aprovação, mas A sempre um pouco melhor no score
        b[pid]["score"] = 0.9
    blk = sc.paired_block(a, b, 1, ("ollama_qwen", "ollama_llama"))
    assert blk["diff_ci95"][0] > 0 and blk["mcnemar"]["p_exact"] == 0.25
    assert sc.INCONCLUSIVE in blk["verdict"] and "não confirma" in blk["verdict"]
    assert "a favor de" not in blk["verdict"]


def test_scoring_meta_records_how_the_run_was_scored(tmp_path):
    root = synthetic_results(tmp_path)
    meta = {"run_id": "r1", "scope": "x", "dataset_hash": "h"}
    (root / "r1" / "meta.json").write_text(json.dumps(meta), encoding="utf-8")
    sc.main(["--run-id", "r1", "--results-root", str(root), "--no-embedding"])
    saved = json.loads((root / "r1" / "meta.json").read_text(encoding="utf-8"))
    assert saved["run_id"] == "r1" and saved["scoring"]["embedding"] == "disabled"
    assert saved["scoring"]["seed"] == sc.DEFAULT_SEED and "scored_at" in saved["scoring"]
    assert not list((root / "r1").glob("*.tmp"))
