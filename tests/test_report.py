"""Gerador do relatório final (Marco 12). Tudo com uma fixture sintética em `tmp_path`: sem rede, sem Ollama.
As respostas dos modelos e os motivos dos juízes da fixture são CANÁRIOS: nenhum pode aparecer no .md nem no .csv."""

import copy
import csv
import io
import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from evalkit import report as rp

Q, L = "ollama_qwen", "ollama_llama"
CANARY_RESPONSE_Q = "CANARIO-RESPOSTA-QWEN-zXq9-nao-pode-vazar"
CANARY_RESPONSE_L = "CANARIO-RESPOSTA-LLAMA-bT4k-nao-pode-vazar"
CANARY_DETAIL = "CANARIO-DETALHE-valor-extraido-pelo-scorer"
CANARY_REASON = "CANARIO-MOTIVO-escrito-pelo-juiz-nao-pode-vazar"
REPO_ROOT = Path(__file__).resolve().parent.parent


def group(n, errors, mean, passed, ci, p50, p95, tps):
    return {"n": n, "scored": n - errors, "errors": errors, "error_kinds": {}, "mean_score": mean,
            "passed": passed, "pass_rate": round(passed / (n - errors), 4), "pass_rate_ci95": ci,
            "latency_p50_ms": p50, "latency_p95_ms": p95, "tokens_per_s_p50": tps, "tokens_per_s_p95": tps}


def make_summary():
    return {
        "run_id": "fx", "seed": 20260, "embedding": "ok", "dataset_hash_run": "ab" * 32, "dataset_hash_now": "ab" * 32,
        "providers": {
            L: {"overall": group(8, 0, 0.5, 4, [0.2, 0.8], 100.0, 200.0, 70.0),
                "by_task_type": {"extraction": group(4, 0, 0.6, 2, [0.15, 0.85], 150.0, 250.0, 120.0),
                                 "classification": group(4, 0, 0.4, 2, [0.15, 0.85], 50.0, 60.0, 45.0)}},
            Q: {"overall": group(8, 0, 0.8125, 7, [0.6, 0.97], 90.0, 180.0, 75.0),
                "by_task_type": {"extraction": group(4, 0, 0.75, 3, [0.3, 0.95], 140.0, 240.0, 125.0),
                                 "classification": group(4, 0, 0.875, 4, [0.5, 1.0], 40.0, 55.0, 48.0)}}},
        "paired": {
            "a": Q, "b": L,
            "overall": {"pairs": 8, "excluded_for_errors": [], "mean_diff_a_minus_b": 0.3125, "diff_ci95": [0.1, 0.5],
                        "mcnemar": {"only_a_passes": 4, "only_b_passes": 1, "p_exact": 0.0312},
                        "verdict": "diferença detectada a favor de ollama_qwen (o IC95% exclui zero)"},
            "by_task_type": {
                "extraction": {"pairs": 4, "excluded_for_errors": [], "mean_diff_a_minus_b": 0.15,
                               "diff_ci95": [0.05, 0.25], "mcnemar": {"only_a_passes": 1, "only_b_passes": 0,
                                                                      "p_exact": 1.0},
                               "verdict": "indicativo, não conclusivo (o IC95% exclui zero, mas o McNemar exato "
                                          "não confirma: p=1.0); nenhum vencedor"},
                "classification": {"pairs": 4, "excluded_for_errors": [], "mean_diff_a_minus_b": 0.475,
                                   "diff_ci95": [-0.1, 0.9], "mcnemar": {"only_a_passes": 2, "only_b_passes": 0,
                                                                          "p_exact": 0.5},
                                   "verdict": "indicativo, não conclusivo (o IC95% da diferença inclui zero); "
                                              "nenhum vencedor declarado"}},
            "both_fail": ["extraction-03"],
            "only_one_fails": [{"prompt_id": "classification-02", "fails": L},
                               {"prompt_id": "instruction-07", "fails": L},
                               {"prompt_id": "extraction-04", "fails": Q}]},
        "embedding_info": {"model": "fx/embedding-model", "revision": "rev1234", "device": "cpu",
                           "max_seq_length": 128},
    }


def make_meta():
    return {"run_id": "fx", "created_at": "2026-01-02T03:04:05+00:00", "ollama_version": "9.9.9",
            "dataset_hash": "ab" * 32, "config_hash": "cd" * 32, "git_commit": "0123456789abcdef", "git_dirty": False,
            "providers": {Q: {"model": "qwen-fx:1b"}, L: {"model": "llama-fx:1b"}},
            "scoring": {"scored_at": "2026-02-03T10:00:00+00:00", "seed": 20260,
                        "embedding_info": {"model": "fx/embedding-model", "revision": "rev1234", "device": "cpu",
                                           "max_seq_length": 128}}}


def make_scores():
    rows = []
    for prov, long_tokens in ((Q, 40), (L, 44)):
        for cat, tokens in (("classification", 3), ("extraction", long_tokens)):
            for i in range(1, 5):
                dirty = prov == L and cat == "extraction" and i == 2
                rows.append({"prompt_id": f"{cat}-0{i}", "provider": prov, "task_type": cat, "score": 1.0,
                             "passed": True, "errored": False, "error_kind": CANARY_DETAIL, "latency_ms": 10.0,
                             "tokens_per_s": 50.0, "completion_tokens": tokens,
                             "details": {"format_clean": not dirty, "labels_found": [CANARY_DETAIL],
                                         "wrong_fields": {"x": CANARY_DETAIL}}})
    return rows


def make_judge():
    def verif(acc, kappa):
        return {"n": 80, "confusion": {"tp": 30, "fp": 10, "fn": 20, "tn": 20}, "accuracy": acc,
                "accuracy_ci95": [0.5, 0.7], "kappa": kappa, "kappa_ci95": [0.0, 0.3], "judge_approval_rate": 0.5,
                "scorer_pass_rate": 0.7}

    def judge(model, acc, kappa):
        return {"model": model, "judge_prompt_version": "v2",
                "calibration": {"verifiable": verif(acc, kappa),
                                "by_task_type": {"classification": {"n": 16, "accuracy": 0.5, "kappa": 0.1},
                                                 "instruction": {"n": 16, "accuracy": 0.9, "kappa": 0.8}},
                                "decision": {"meets_criterion": False, "label": "NÃO CONFIÁVEL: fx"},
                                "error_analysis": {"false_approvals": [{"reason": CANARY_REASON}]}},
                "pairwise": {"position_bias_index": 0.6, "order_inconsistency": 0.7,
                             "final_preference": {Q: 3, L: 2, "tie": 43}},
                "open_tasks": {"summarization": {"pooled": {"spearman": 0.3, "n": 16}},
                               "instruction_extra": {"calibration": {"n": 16, "accuracy": 0.9375, "kappa": 0.76,
                                                                     "kappa_ci95": [0.0, 1.0]}}}}

    own = {"false_approvals_on_own_answers": {"scorer_failed": 6, "judge_approved": 1},
           "false_approvals_on_other_answers": {"scorer_failed": 15, "judge_approved": 6}}
    return {"criterion": {"kappa_min": 0.6, "accuracy_min": 0.8, "approve_min_overall": 4, "set": "fx"},
            "judges": {Q: judge("qwen-fx:1b", 0.55, 0.1), L: judge("llama-fx:1b", 0.75, 0.2)},
            "cross_judge": {"pointwise": {"approval_agreement": 0.46, "approval_kappa": 0.04,
                                          "spearman_overall": 0.15},
                            "pairwise": {"preference_agreement": 0.71, "preference_kappa": 0.14}},
            "self_preference": {Q: own, L: own}}


def make_human():
    items = []
    for n, (pid, tgt, tt, grp) in enumerate([
            ("classification-01", L, "classification", "agree"), ("instruction-07", L, "instruction", "disagree"),
            ("summarization-02", L, "summarization", "summarization"),
            ("summarization-05", Q, "summarization", "summarization"),
            ("tool_use-01", Q, "tool_use", "extra_tool_use")], 1):
        items.append({"n": n, "prompt_id": pid, "target": tgt, "task_type": tt, "group": grp, "scorer_passed": True,
                      "human_agrees_scorer": True, "human_passed": True, "judge_approved": {Q: False, L: True}})
    return {"run_id": "fx", "seed": 20260, "statement": "concordo com todos", "checked_directly": [2, 3],
            "caveats": [{"items": [2], "text": "motivo acidental no scorer"}], "items": items}


def write_run(root: Path, *, judge=True, human=True, summary=None, scores=None, human_obj=None) -> Path:
    run = root / "fx"
    run.mkdir(parents=True)
    (run / "summary.json").write_text(json.dumps(summary or make_summary()), encoding="utf-8")
    (run / "meta.json").write_text(json.dumps(make_meta()), encoding="utf-8")
    rows = scores or make_scores()
    (run / "scores.jsonl").write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")
    if judge:
        (run / "judge_summary.json").write_text(json.dumps(make_judge()), encoding="utf-8")
    if human:
        (run / "human_verdicts.json").write_text(json.dumps(human_obj or make_human()), encoding="utf-8")
    for prov, canary in ((Q, CANARY_RESPONSE_Q), (L, CANARY_RESPONSE_L)):
        recs = [{"prompt_id": f"{c}-0{i}", "response": f"{canary} {c} {i}", "status": 200}
                for c in ("classification", "extraction") for i in range(1, 5)]
        (run / f"{prov}.jsonl").write_text("\n".join(json.dumps(r) for r in recs) + "\n", encoding="utf-8")
    return run


@pytest.fixture
def results_root(tmp_path):
    root = tmp_path / "results"
    write_run(root)
    return root


def generate(results_root, out_dir, gpu="GPU-FX"):
    md, cs = rp.execute("fx", results_root=results_root, out_dir=out_dir, gpu_name=gpu, out=lambda _m: None)
    return md.read_text(encoding="utf-8"), cs.read_text(encoding="utf-8")


# --------------------------------------------------------------------------- determinismo e estrutura
def test_same_input_gives_identical_output(results_root, tmp_path):
    md1, csv1 = generate(results_root, tmp_path / "o1")
    md2, csv2 = generate(results_root, tmp_path / "o2")
    assert md1 == md2 and csv1 == csv2
    assert (tmp_path / "o1" / "fase3-qwen-vs-llama.md").read_bytes() == (tmp_path / "o2" / "fase3-qwen-vs-llama.md").read_bytes()


def test_date_comes_from_meta_not_from_the_clock(results_root, tmp_path):
    md, _ = generate(results_root, tmp_path / "o")
    assert "2026-02-03" in md  # scoring.scored_at do meta.json


def test_sections_are_present_in_order(results_root, tmp_path):
    md, _ = generate(results_root, tmp_path / "o")
    heads = re.findall(r"^## (\d)\. (.+)$", md, re.M)
    assert [h[0] for h in heads] == [str(i) for i in range(1, 10)]
    assert [h[1] for h in heads] == ["Resumo", "Escopo e avisos", "Método", "Resultados", "Latência e tok/s",
                                     "Juiz local", "Revisão humana", "Limitações", "Próximos passos"]
    summary_lines = md.split("## 1. Resumo")[1].split("## 2.")[0].strip().splitlines()
    assert len([ln for ln in summary_lines if ln.strip()]) == 5


def test_csv_columns_and_known_row(results_root, tmp_path):
    _, text = generate(results_root, tmp_path / "o")
    rows = list(csv.DictReader(io.StringIO(text)))
    assert list(rows[0]) == list(rp.CSV_COLUMNS)
    assert len(rows) == 2 * 3  # 2 provedores x (geral + 2 categorias)
    qwen_all = next(r for r in rows if r["provider"] == Q and r["task_type"] == "all")
    assert qwen_all == {"provider": Q, "task_type": "all", "n": "8", "errors": "0", "mean_score": "0.8125",
                        "pass_rate": "0.875", "ci95_low": "0.6", "ci95_high": "0.97", "latency_p50_ms": "90.0",
                        "latency_p95_ms": "180.0", "tokens_per_s_p50": "75.0"}


def test_results_and_paired_tables_have_known_values(results_root, tmp_path):
    md, _ = generate(results_root, tmp_path / "o")
    assert "| Qwen | geral | 8 | 0 | 0,81 | 87,5% | 60% a 97% |" in md
    assert "| Llama | extraction | 4 | 0 | 0,60 | 50,0% | 15% a 85% |" in md
    assert "| geral | 8 | 0,31 | 0,10 a 0,50 | 4 x 1 | 0,0312 | diferença detectada |" in md
    assert "| classification | 4 | 0,47 | -0,10 a 0,90 | 2 x 0 | 0,5000 | indicativo, não conclusivo (IC inclui zero) |" in md
    assert ("| extraction | 4 | 0,15 | 0,05 a 0,25 | 1 x 0 | 1,0000 | indicativo, não conclusivo "
            "(IC exclui zero, McNemar não confirma) |") in md
    assert "- Os dois reprovam: `extraction-03`." in md
    assert "- Só o Llama reprova (2): `classification-02`, `instruction-07`." in md
    assert "- Só o Qwen reprova (1): `extraction-04`." in md
    assert "diferença detectada a favor do Qwen" in md


def test_inconclusive_categories_never_name_a_winner(results_root, tmp_path):
    md, _ = generate(results_root, tmp_path / "o")
    rows = [ln for ln in md.splitlines() if ln.startswith("| classification | 4 | 0,47") or ln.startswith("| extraction | 4 | 0,15")]
    assert len(rows) == 2 and all("indicativo, não conclusivo" in r for r in rows)
    assert not any(w in r for r in rows for w in ("melhor", "vence", "a favor"))
    assert "nenhuma comparação é conclusiva" in md or "comparações são conclusivas" in md


def test_paired_status_labels():
    assert rp.paired_status({"verdict": "diferença detectada a favor de x"}) == ("diferença detectada", True)
    label, ok = rp.paired_status({"verdict": "indicativo, não conclusivo (…)", "diff_ci95": [-1, 1]})
    assert ok is False and label.startswith("indicativo, não conclusivo")


def test_format_clean_section_counts_and_ids(results_root, tmp_path):
    md, _ = generate(results_root, tmp_path / "o")
    assert "| Qwen | 8 | 0 | nenhum |" in md
    assert "| Llama | 8 | 1 | `extraction-02` |" in md


def test_latency_marks_short_categories_not_comparable(results_root, tmp_path):
    md, _ = generate(results_root, tmp_path / "o")
    assert "| Qwen | classification | 40 | 55 | 48,0 | não (resposta curta) |" in md
    assert "| Qwen | extraction | 140 | 240 | 125,0 | sim |" in md
    assert rp.short_categories(rp.load_score_rows(results_root / "fx")) == {"classification"}


def test_method_section_lists_environment(results_root, tmp_path):
    md, _ = generate(results_root, tmp_path / "o")
    for needle in ("`abababababab`", "Ollama 9.9.9", "`qwen-fx:1b`", "`fx/embedding-model`", "revision `rev1234`",
                   "`0123456789abcdef`", "GPU-FX", "`cdcdcdcdcdcd`"):
        assert needle in md
    assert "alterações não commitadas" not in md  # git_dirty falso na fixture


def test_gpu_is_reported_as_unavailable_when_missing(results_root, tmp_path):
    md, _ = generate(results_root, tmp_path / "o", gpu=None)
    assert "não informada" in md


# --------------------------------------------------------------------------- avisos obrigatórios
def test_every_mandatory_warning_is_in_section_2(results_root, tmp_path):
    md, _ = generate(results_root, tmp_path / "o")
    section = md.split("## 2. Escopo e avisos")[1].split("## 3.")[0]
    assert len(rp.WARNINGS) == 6
    for w in rp.WARNINGS:
        assert w in section
    assert "NÃO mede a perda em relação a um modelo de mercado" in section
    assert "qwen2.5:3b" in rp.W_SCOPE and "llama3.2:3b" in rp.W_SCOPE
    assert "8 prompts por categoria" in section and "indicativo, não conclusivo" in section
    assert "`reasoning`" in section and "2,4 GHz" in section and "ends_with" in section


def test_warnings_are_repeated_where_relevant(results_root, tmp_path):
    md, _ = generate(results_root, tmp_path / "o")
    assert md.count(rp.W_LATENCY) >= 2  # Seção 2 e Seção 5
    assert "(aviso 5)" in md and "(aviso 3)" in md and "(aviso 6)" in md
    assert "nenhuma chamada à Anthropic" in md.split("## 2.")[0]  # já no resumo


def test_judge_section_reports_numbers_and_disclosures(results_root, tmp_path):
    md, _ = generate(results_root, tmp_path / "o")
    sec = md.split("## 6. Juiz local")[1].split("## 7.")[0]
    assert "| Qwen-juiz | 80 | 55,0% | 50% a 70% | 0,10 | 0,00 a 0,30 | 50,0% | 30/10/20/20 |" in sec
    assert "acurácia 70,0%" in sec  # linha de base "aprovar tudo" = taxa de aprovação do scorer
    assert "abaixo da linha" in sec and "acima da linha" in sec
    assert "NÃO CONFIÁVEL" in sec
    assert "Viés de posição" in sec and "| Qwen-juiz | 60% | 70% | 3 | 2 | 43 |" in sec
    assert "v1 para a v2" in sec and "`v2`" in sec and "NÃO foi ajustado a partir de concordância" in sec
    assert "Autopreferência: sem evidência suficiente" in sec
    assert "Injeção de prompt: sem evidência suficiente" in sec
    low = sec.lower()
    assert "sem viés" not in low and "resistente" not in low.replace("não se afirma resistência", "")
    assert "kappa >= 0,6" in sec and "80%" in sec
    assert "instruction" in sec and "0,76" in sec


def test_human_section_has_agreement_tables_and_caveats(results_root, tmp_path):
    md, _ = generate(results_root, tmp_path / "o")
    sec = md.split("## 7. Revisão humana")[1].split("## 8.")[0]
    assert "| Todos os itens do sorteio | 4 | 4/4 |" in sec
    assert "| Summarization | 2 | 2/2 |" in sec and "| Verificáveis | 2 | 2/2 |" in sec
    assert "FORA da tabela" in sec and "amostra pequena" in sec
    assert "| Llama-juiz | 2/2 | 2/2 | 4/4 | 1/1 |" in sec
    assert "| Qwen-juiz | 0/2 | 0/2 | 0/4 | 0/1 |" in sec
    assert "Item(ns) 2 (`instruction-07`): motivo acidental no scorer" in sec


def test_optional_inputs_missing_are_reported_not_crashing(tmp_path):
    root = tmp_path / "r"
    write_run(root, judge=False, human=False)
    md, _ = generate(root, tmp_path / "o")
    assert "`judge_summary.json` ausente" in md and "`human_verdicts.json` ausente" in md
    assert "O juiz local não foi executado" in md


# --------------------------------------------------------------------------- segurança
def test_no_response_text_in_report_or_csv(results_root, tmp_path):
    md, cs = generate(results_root, tmp_path / "o")
    for canary in (CANARY_RESPONSE_Q, CANARY_RESPONSE_L, CANARY_DETAIL, CANARY_REASON, "CANARIO"):
        assert canary not in md
        assert canary not in cs


def test_report_has_no_absolute_paths(results_root, tmp_path):
    md, cs = generate(results_root, tmp_path / "o")
    for text in (md, cs):
        assert not re.search(r"[A-Za-z]:[\\/]", text) and "/Users/" not in text and "/home/" not in text


def test_sanitize_score_row_drops_everything_that_can_carry_response_text():
    raw = make_scores()[0]
    safe = rp.sanitize_score_row(raw)
    assert set(safe) == set(rp.SCORE_FIELDS) | {"format_clean"}
    assert safe["format_clean"] is True
    assert CANARY_DETAIL not in json.dumps(safe)
    assert "details" not in safe and "error_kind" not in safe


def test_load_score_rows_never_returns_details_values(results_root):
    rows = rp.load_score_rows(results_root / "fx")
    assert rows and CANARY_DETAIL not in json.dumps(rows)


def test_judge_view_drops_reasons():
    view = rp.judge_view(make_judge())
    assert CANARY_REASON not in json.dumps(view)
    assert view["judges"][Q]["verifiable"]["accuracy"] == 0.55


def test_leak_guard_refuses_to_write_when_a_response_would_appear(tmp_path):
    root = tmp_path / "r"
    human = make_human()
    human["caveats"][0]["text"] = f"nota: {CANARY_RESPONSE_Q} classification 1"  # texto de resposta no relatório
    write_run(root, human_obj=human)
    out_dir = tmp_path / "o"
    with pytest.raises(rp.ReportError, match="apareceriam no relatório"):
        rp.execute("fx", results_root=root, out_dir=out_dir, gpu_name="g", out=lambda _m: None)
    assert not out_dir.exists()


def test_find_leaks_counts_only_long_enough_texts():
    assert rp.find_leaks(["abc " + "x" * 20 + " def"], ["x" * 20]) == 1
    assert rp.find_leaks(["abc"], ["x" * 20]) == 0
    texts = rp.raw_texts({"p": [{"response": "42"}, {"response": "r" * 20}]}, {"a": [{"reason": "m" * 20}]})
    assert sorted(texts) == ["m" * 20, "r" * 20]  # "42" é curto demais para ser testável


# --------------------------------------------------------------------------- erros de entrada
def test_missing_summary_or_scores_is_a_clear_error(tmp_path):
    root = tmp_path / "r"
    run = write_run(root)
    (run / "summary.json").unlink()
    with pytest.raises(rp.ReportError, match="summary.json"):
        rp.execute("fx", results_root=root, out_dir=tmp_path / "o", out=lambda _m: None)
    root2 = tmp_path / "r2"
    run2 = write_run(root2)
    (run2 / "scores.jsonl").unlink()
    with pytest.raises(rp.ReportError, match="scores.jsonl"):
        rp.execute("fx", results_root=root2, out_dir=tmp_path / "o", out=lambda _m: None)


def test_unknown_run_is_a_clear_error(tmp_path):
    with pytest.raises(rp.ReportError, match="run não encontrado"):
        rp.execute("nada", results_root=tmp_path, out_dir=tmp_path / "o", out=lambda _m: None)


def test_summary_and_scores_that_disagree_are_refused(tmp_path):
    root = tmp_path / "r"
    write_run(root, scores=make_scores()[:-1])
    with pytest.raises(rp.ReportError, match="divergem"):
        rp.execute("fx", results_root=root, out_dir=tmp_path / "o", out=lambda _m: None)


def test_invalid_json_is_a_clear_error(tmp_path):
    root = tmp_path / "r"
    run = write_run(root)
    (run / "meta.json").write_text("{nao e json", encoding="utf-8")
    with pytest.raises(rp.ReportError, match="meta.json"):
        rp.execute("fx", results_root=root, out_dir=tmp_path / "o", out=lambda _m: None)


def test_cli_returns_1_on_error_and_0_on_success(tmp_path, monkeypatch, capsys):
    assert rp.main(["--run-id", "run-que-nao-existe-xyz", "--out-dir", str(tmp_path / "o")]) == 1
    assert "erro:" in capsys.readouterr().err
    root = tmp_path / "r"
    write_run(root)
    monkeypatch.setattr(rp, "RESULTS_ROOT", root)
    original = rp.execute
    monkeypatch.setattr(rp, "execute", lambda run_id, **kw: original(run_id, results_root=root, **kw))
    assert rp.main(["--run-id", "fx", "--out-dir", str(tmp_path / "o"), "--gpu", "GPU-X"]) == 0
    assert "GPU-X" in (tmp_path / "o" / "fase3-qwen-vs-llama.md").read_text(encoding="utf-8")


def test_detect_gpu_handles_missing_tool_and_failures(monkeypatch):
    monkeypatch.setattr(rp.shutil, "which", lambda _n: None)
    assert rp.detect_gpu() is None
    monkeypatch.setattr(rp.shutil, "which", lambda _n: "nvidia-smi")

    class Res:
        returncode, stdout = 0, "NVIDIA Fake 9000\nOutra\n"

    monkeypatch.setattr(rp.subprocess, "run", lambda *a, **k: Res())
    assert rp.detect_gpu() == "NVIDIA Fake 9000"

    def boom(*a, **k):
        raise OSError("sem permissão")

    monkeypatch.setattr(rp.subprocess, "run", boom)
    assert rp.detect_gpu() is None


def test_formatters():
    assert rp.pct(None) == "n/d" and rp.num(None) == "n/d" and rp.ci_text(None) == "n/d"
    assert rp.pct(0.875) == "87,5%" and rp.num(0.1234, 3) == "0,123"
    assert rp.order_categories(["tool_use", "zzz", "classification"]) == ["classification", "tool_use", "zzz"]
    assert rp.median([]) is None and rp.median([1, 3]) == 2.0 and rp.median([5, 1, 3]) == 3.0


def test_inconclusive_overall_does_not_claim_a_winner(tmp_path):
    summary = copy.deepcopy(make_summary())
    summary["paired"]["overall"]["verdict"] = "indicativo, não conclusivo (o IC95% da diferença inclui zero)"
    root = tmp_path / "r"
    write_run(root, summary=summary)
    md, _ = generate(root, tmp_path / "o")
    resumo = md.split("## 2.")[0]
    assert "diferença detectada a favor" not in resumo
    assert "indicativo, não conclusivo" in resumo


# --------------------------------------------------------------------------- reports/ é versionado
@pytest.mark.skipif(shutil.which("git") is None, reason="git indisponível")
def test_reports_dir_is_not_gitignored():
    for name in ("reports/fase3-qwen-vs-llama.md", "reports/fase3-qwen-vs-llama.csv"):
        res = subprocess.run(["git", "check-ignore", "-q", name], cwd=REPO_ROOT, check=False)
        assert res.returncode == 1, f"{name} está ignorado pelo git"
