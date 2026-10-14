"""Relatório do juiz: calibração, autopreferência, juízes cruzados, tarefas abertas e revisão humana (Marco 11).
Tudo com dados sintéticos em `tmp_path`: sem rede, sem Ollama."""

import json
import random
import re
import zlib

import pytest

from evalkit import judge_report as jr
from evalkit.score import ScoreError
from evalkit.validate_dataset import PROMPTS_DIR, load_dataset

Q, L = "ollama_qwen", "ollama_llama"


@pytest.fixture(scope="module")
def dataset():
    return load_dataset(PROMPTS_DIR).by_id()


def prow(pid, target, overall, passed=True, *, verifiable=True, judge_error=False, task_type="extraction", score=None,
         errored=False):
    return {"prompt_id": pid, "target": target, "task_type": task_type, "verifiable": verifiable,
            "judge_error": judge_error, "overall": None if judge_error else overall, "scores": {}, "reason": "motivo",
            "scorer_passed": passed, "scorer_score": (1.0 if passed else 0.0) if score is None else score,
            "scorer_errored": errored, "scorer_details": {}, "chars": 10}


def pw(pid, first, second, judge_error=False):
    """Duas rodadas do pareado de um prompt; `first`/`second` = quem ganhou cada uma."""
    return [{"key": f"pairwise|{pid}|{n}", "kind": "pairwise", "prompt_id": pid, "round": n, "a": Q, "b": L,
             "winner": "A", "winner_model": w, "judge_error": judge_error} for n, w in ((1, first), (2, second))]


# --------------------------------------------------------------------------- calibração
def test_calibration_block_known_values_and_exclusions():
    rows = [prow(f"p-0{i}", Q, 5, True) for i in range(1, 4)]            # tp
    rows += [prow("p-04", Q, 5, False)]                                    # fp
    rows += [prow("p-05", Q, 2, True)]                                     # fn
    rows += [prow(f"p-0{i}", Q, 1, False) for i in range(6, 8)]           # tn
    rows += [prow("p-08", Q, 0, True, judge_error=True)]                   # fora: judge_error
    rows += [prow("p-09", Q, 5, True, errored=True)]                       # fora: scorer com erro
    b = jr.calibration_block(rows, seed=1)
    assert b["n"] == 7 and b["judge_errors"] == 1 and b["confusion"] == {"tp": 3, "fp": 1, "fn": 1, "tn": 2}
    assert b["accuracy"] == pytest.approx(5 / 7, abs=1e-4)
    assert b["precision"] == pytest.approx(0.75) and b["recall"] == pytest.approx(0.75)
    assert b["kappa"] == pytest.approx(5 / 12, abs=1e-4)  # (5/7 - 25/49) / (24/49)
    assert b["accuracy_ci95"][0] < b["accuracy"] < b["accuracy_ci95"][1]
    assert b["judge_approval_rate"] == pytest.approx(4 / 7, abs=1e-4)


def test_decision_labels_a_judge_that_meets_or_misses_the_criterion():
    good = [prow(f"p-{i:02d}", Q, 5 if i % 3 else 1, passed=bool(i % 3)) for i in range(1, 31)]
    bad = [prow(f"p-{i:02d}", Q, 5, passed=bool(i % 3)) for i in range(1, 31)]  # aprova tudo
    ok = jr.decision(jr.calibration_block(good, 1))
    no = jr.decision(jr.calibration_block(bad, 1))
    assert ok["meets_criterion"] and ok["label"] == jr.TRUSTED_LABEL
    assert not no["meets_criterion"] and "NÃO CONFIÁVEL" in no["label"]
    assert no["criterion"] == {"kappa_min": 0.6, "accuracy_min": 0.8, "approve_min_overall": 4}


def test_error_analysis_lists_false_approvals_and_rejections():
    rows = [prow("p-01", Q, 5, False), prow("p-02", L, 1, True), prow("p-03", Q, 5, True), prow("p-04", L, 1, False)]
    ea = jr.error_analysis(rows)
    assert ea["counts"] == {"true_approve": 1, "false_approve": 1, "true_reject": 1, "false_reject": 1}
    assert [r["prompt_id"] for r in ea["false_approvals"]] == ["p-01"]
    assert [r["prompt_id"] for r in ea["false_rejections"]] == ["p-02"]
    assert ea["overall_when_scorer_failed"] == {"1": 1, "5": 1}


# --------------------------------------------------------------------------- autopreferência e juízes cruzados
def self_pref_inputs():
    # o juiz Q dá 5 às respostas do Q e 3 às do L; o juiz L dá 5 às do L e 3 às do Q
    rows = {
        Q: [prow("p-01", Q, 5), prow("p-02", Q, 5), prow("p-01", L, 3), prow("p-02", L, 3)],
        L: [prow("p-01", L, 5), prow("p-02", L, 5), prow("p-01", Q, 3), prow("p-02", Q, 3)],
    }
    recs = {
        Q: pw("p-01", Q, Q) + pw("p-02", Q, Q) + pw("p-03", "tie", "tie") + pw("p-04", L, L),
        L: pw("p-01", L, L) + pw("p-02", "tie", "tie") + pw("p-03", L, L) + pw("p-04", L, L),
    }
    return rows, recs


def test_self_preference_known_values():
    rows, recs = self_pref_inputs()
    sp = jr.self_preference(rows, recs, (Q, L))
    for judge in (Q, L):
        b = sp[judge]
        assert b["pointwise_own_gap"] == pytest.approx(2.0)
        assert b["pointwise_leniency_gap_on_other_model"] == pytest.approx(-2.0)
        assert b["pointwise_net"] == pytest.approx(4.0)
        assert b["pairwise_own_gap"] == pytest.approx(0.5)  # o mesmo contraste para os dois, por construção
    assert sp[Q]["pairwise_win_rate_of_own_by_this_judge"] == pytest.approx(0.625)
    assert sp[Q]["pairwise_win_rate_of_own_by_other_judge"] == pytest.approx(0.125)
    assert sp[L]["pairwise_win_rate_of_own_by_this_judge"] == pytest.approx(0.875)
    assert sp[L]["pairwise_win_rate_of_own_by_other_judge"] == pytest.approx(0.375)
    assert "MESMO contraste" in sp["note"]


def test_self_preference_false_approvals_on_own_vs_other_answers():
    rows = {
        Q: [prow("p-01", Q, 5, False), prow("p-02", Q, 1, False), prow("p-03", L, 5, False), prow("p-04", L, 5, False)],
        L: [prow("p-01", Q, 5, False), prow("p-03", L, 1, False)],
    }
    sp = jr.self_preference(rows, {Q: [], L: []}, (Q, L))
    assert sp[Q]["false_approvals_on_own_answers"] == {"scorer_failed": 2, "judge_approved": 1, "rate": 0.5}
    assert sp[Q]["false_approvals_on_other_answers"] == {"scorer_failed": 2, "judge_approved": 2, "rate": 1.0}


def test_cross_judge_agreement_known_values_and_no_score_average():
    rows = {Q: [prow(f"p-0{i}", Q, o) for i, o in enumerate([5, 4, 2, 1], 1)],
            L: [prow(f"p-0{i}", Q, o) for i, o in enumerate([5, 2, 2, 4], 1)]}
    recs = {Q: pw("p-01", Q, Q) + pw("p-02", Q, L), L: pw("p-01", Q, Q) + pw("p-02", L, L)}
    cj = jr.cross_judge(rows, recs, (Q, L))
    p = cj["pointwise"]
    assert p["n"] == 4 and p["exact_overall_agreement"] == 0.5 and p["within_one_point"] == 0.5
    assert p["approval_agreement"] == 0.5  # aprovações: (T,T) (T,F) (F,F) (F,T)
    assert p["approval_kappa"] == pytest.approx(0.0)
    assert p["mean_overall"] == {Q: 3.0, L: 3.25}
    w = cj["pairwise"]  # finais: p-01 Q/Q; p-02 tie (Q,L) contra L (L,L)
    assert w["n"] == 2 and w["preference_agreement"] == 0.5
    assert "NÃO são promediadas" in cj["note"]


# --------------------------------------------------------------------------- tarefas abertas
def test_summarization_block_uses_spearman_and_carries_the_reliability_label():
    rows = [prow(f"summarization-0{i}", t, o, True, task_type="summarization", verifiable=False, score=s)
            for i, (o, s) in enumerate([(2, 0.4), (3, 0.6), (4, 0.8), (5, 0.9)], 1) for t in (Q, L)]
    block = jr.open_tasks_block(rows, jr.UNTRUSTED_LABEL)
    assert block["pooled"]["n"] == 8 and block["pooled"]["spearman"] == pytest.approx(1.0)
    assert block["reliability"] == jr.UNTRUSTED_LABEL and "16 respostas" in block["warning"]
    flat = [prow(f"summarization-0{i}", Q, 4, True, task_type="summarization", verifiable=False, score=i / 10)
            for i in range(1, 6)]
    assert jr.open_tasks_block(flat, jr.UNTRUSTED_LABEL)["pooled"]["spearman"] is None  # juiz sempre dá 4


def test_instruction_extra_is_separate_from_the_summarization_block():
    rows = [prow(f"instruction-0{i}", Q, 5 if i % 2 else 1, bool(i % 2), task_type="instruction") for i in range(1, 9)]
    extra = jr.instruction_extra(rows, 1)
    assert extra["calibration"]["accuracy"] == 1.0 and extra["spearman_overall_vs_scorer"] == pytest.approx(1.0)
    assert "já cobre" in extra["note"]


# --------------------------------------------------------------------------- revisão humana
def test_stratified_pick_spreads_categories_and_is_seeded():
    cands = [prow(f"{c}-{i:02d}", Q, 5, task_type=c) for c in ("a", "b", "c", "d", "e") for i in range(1, 9)]
    one = jr.stratified_pick(random.Random(5), cands, 4)
    two = jr.stratified_pick(random.Random(5), cands, 4)
    assert [r["prompt_id"] for r in one] == [r["prompt_id"] for r in two]
    assert len({r["task_type"] for r in one}) == 4
    other = jr.stratified_pick(random.Random(6), cands, 4)
    assert [r["prompt_id"] for r in other] != [r["prompt_id"] for r in one]
    again = jr.stratified_pick(random.Random(5), cands, 4, already=one)
    assert not {r["prompt_id"] for r in again} & {r["prompt_id"] for r in one}


def test_stratified_pick_with_fewer_candidates_than_requested():
    assert len(jr.stratified_pick(random.Random(1), [prow("a-01", Q, 5, task_type="a")], 4)) == 1
    assert jr.stratified_pick(random.Random(1), [], 4) == []


def test_review_prefers_qwen_false_approvals_among_the_disagreements():
    cats = ("extraction", "classification", "reasoning", "instruction", "tool_use")
    rows = {Q: [], L: []}
    for c in cats:
        for i in range(1, 9):
            pid = f"{c}-{i:02d}"
            for target in (Q, L):
                for j in (Q, L):
                    rows[j].append(prow(pid, target, 5, True, task_type=c))  # todos concordam
    # 3 aprovações indevidas de respostas do Qwen por UM juiz (o scorer reprovou)
    for pid in ("extraction-01", "reasoning-02", "tool_use-03"):
        for r in rows[L]:
            if r["prompt_id"] == pid and r["target"] == Q:
                r["scorer_passed"] = False
        for r in rows[Q]:
            if r["prompt_id"] == pid and r["target"] == Q:
                r["scorer_passed"] = False
                r["overall"] = 1  # este juiz concorda com o scorer; o outro (L) aprova indevidamente
    # e 8 discordâncias em respostas do Llama: sem a preferência, o Qwen poderia ficar de fora
    for i in range(1, 9):
        for j in (Q, L):
            for r in rows[j]:
                if r["prompt_id"] == f"classification-{i:02d}" and r["target"] == L:
                    r["scorer_passed"] = False
    for t in (Q, L):
        for i in range(1, 9):
            for j in (Q, L):
                rows[j].append(prow(f"summarization-{i:02d}", t, 4, True, task_type="summarization", verifiable=False))
    for seed in range(20):
        picks = jr.pick_review_items(rows, seed=seed)
        assert len(picks["agree"]) == 4 and len(picks["disagree"]) == 4 and len(picks["summarization"]) == 8
        qwen_false = [v for v in picks["disagree"] if v["target"] == Q and not v["scorer_passed"]]
        assert len(qwen_false) >= 2, seed  # aprovações indevidas de respostas do Qwen sempre entram (piso de 2)
        assert all(all(v["judge_ok"].values()) for v in picks["agree"])
        assert all(not all(v["judge_ok"].values()) for v in picks["disagree"])
        assert [sum(v["target"] == t for v in picks["summarization"]) for t in (Q, L)] == [4, 4]


def test_fenced_text_cannot_close_the_code_block():
    out = jr.fenced("a ```` b")
    assert out.startswith("````text\n") and out.endswith("\n````") and out.count("````") == 2


# --------------------------------------------------------------------------- ponta a ponta (arquivos sintéticos)
def build_run(tmp_path, dataset, point, pair, judges=(Q, L)):
    """Run sintético em disco: respostas, scores, julgamentos e metas. `point(judge, row) -> nota` e
    `pair(judge, pid) -> (rodada1, rodada2)` definem o que cada juiz diz."""
    run_dir = tmp_path / "results" / "r1"
    (run_dir / "judge").mkdir(parents=True)
    score_rows = []
    for provider in (Q, L):
        recs = []
        for pid, p in sorted(dataset.items()):
            recs.append({"prompt_id": pid, "task_type": p.task_type, "provider": provider, "status": 200,
                         "response": f"resposta {pid} de {provider}", "latency_ms": 5.0, "usage": None})
            passed = zlib.crc32(f"{pid}{provider}".encode()) % 4 != 0
            score_rows.append({"prompt_id": pid, "provider": provider, "task_type": p.task_type,
                               "score": 1.0 if passed else 0.0, "passed": passed, "errored": False,
                               "error_kind": None, "details": {"id": pid}})
        (run_dir / f"{provider}.jsonl").write_text("".join(json.dumps(r) + "\n" for r in recs), encoding="utf-8")
    (run_dir / "scores.jsonl").write_text("".join(json.dumps(r) + "\n" for r in score_rows), encoding="utf-8")
    by_key = {(r["prompt_id"], r["provider"]): r for r in score_rows}
    for j in judges:
        lines = []
        for (pid, provider), row in sorted(by_key.items()):
            lines.append({"key": f"pointwise|{pid}|{provider}", "kind": "pointwise", "prompt_id": pid, "judge": j,
                          "target": provider, "overall": point(j, row), "scores": {"x": 3}, "reason": "porque sim",
                          "judge_error": False, "attempts": 1})
        for pid in sorted(dataset):
            w1, w2 = pair(j, pid)
            lines += [dict(r, judge=j, attempts=1) for r in pw(pid, w1, w2)]
        (run_dir / "judge" / f"{j}.jsonl").write_text("".join(json.dumps(r) + "\n" for r in lines), encoding="utf-8")
        (run_dir / "judge" / f"{j}.meta.json").write_text(json.dumps(
            {"model": "m", "judge_prompt_hash": "h" * 64, "judge_prompt_version": "v2", "scope": "escopo de teste"}),
            encoding="utf-8")
    return run_dir.parent


def noisy(judge, row):
    return zlib.crc32(f"{judge}{row['prompt_id']}{row['provider']}".encode()) % 5 + 1


def flipper(judge, pid):
    return (Q, L)  # sempre a posição A: troca de lado a cada prompt


def test_end_to_end_report_writes_summary_and_review(tmp_path, dataset):
    root = build_run(tmp_path, dataset, noisy, flipper)
    summary = jr.execute("r1", root, out=lambda s: None)
    run_dir = root / "r1"
    on_disk = json.loads((run_dir / "judge_summary.json").read_text(encoding="utf-8"))
    assert on_disk["run_id"] == "r1" and set(on_disk["judges"]) == {Q, L} and summary["pair"] == [Q, L]
    for j in (Q, L):
        d = on_disk["judges"][j]
        assert d["counts"]["pointwise"]["total"] == 96 and d["counts"]["pairwise"]["total"] == 96
        assert d["calibration"]["verifiable"]["n"] == 80
        assert d["pairwise"]["position_bias_index"] == 1.0 and d["pairwise"]["final_preference"]["tie"] == 48
        assert d["calibration"]["decision"]["criterion"]["kappa_min"] == 0.6
        assert d["open_tasks"]["summarization"]["pooled"]["n"] == 16
    assert on_disk["cross_judge"]["pointwise"]["n"] == 96 and on_disk["self_preference"] is not None
    review = (run_dir / "judge_review.md").read_text(encoding="utf-8")
    assert len(re.findall(r"^### \d+\. ", review, re.M)) == 16
    assert len(re.findall(r"\| summarization$", review, re.M)) == 8
    assert len(re.findall(rf"resposta de {Q} \| summarization", review)) == 4
    assert len(re.findall(rf"resposta de {L} \| summarization", review)) == 4
    assert "Seu veredito" in review and "Concorda com o scorer?" in review and "details" in review


def test_review_is_deterministic_for_a_seed(tmp_path, dataset):
    root = build_run(tmp_path, dataset, noisy, flipper)
    jr.execute("r1", root, out=lambda s: None)
    first = (root / "r1" / "judge_review.md").read_text(encoding="utf-8")
    jr.execute("r1", root, out=lambda s: None)
    assert (root / "r1" / "judge_review.md").read_text(encoding="utf-8") == first
    jr.execute("r1", root, seed=99, out=lambda s: None)
    assert (root / "r1" / "judge_review.md").read_text(encoding="utf-8") != first


def test_oracle_judge_is_usable_and_an_inverted_one_is_not(tmp_path, dataset):
    def oracle(judge, row):
        return 5 if zlib.crc32(f"{row['prompt_id']}{row['provider']}".encode()) % 4 != 0 else 1

    root = build_run(tmp_path / "a", dataset, oracle, lambda j, p: (Q, Q))
    summary = jr.execute("r1", root, out=lambda s: None)
    for j in (Q, L):
        dec = summary["judges"][j]["calibration"]["decision"]
        assert dec["meets_criterion"] and dec["kappa"] == 1.0 and dec["label"] == jr.TRUSTED_LABEL
        assert summary["judges"][j]["open_tasks"]["summarization"]["reliability"] == jr.TRUSTED_LABEL
        assert summary["judges"][j]["pairwise"]["position_bias_index"] == 0.0
    inverted = build_run(tmp_path / "b", dataset, lambda j, row: 6 - oracle(j, row), lambda j, p: (Q, Q))
    bad = jr.execute("r1", inverted, out=lambda s: None)
    for j in (Q, L):
        dec = bad["judges"][j]["calibration"]["decision"]
        assert not dec["meets_criterion"] and dec["kappa"] < 0 and "NÃO CONFIÁVEL" in dec["label"]


def test_report_with_a_single_judge_skips_the_cross_judge_sections(tmp_path, dataset):
    root = build_run(tmp_path, dataset, noisy, flipper, judges=(Q,))
    summary = jr.execute("r1", root, out=lambda s: None)
    assert list(summary["judges"]) == [Q] and summary["cross_judge"] is None and summary["self_preference"] is None


def test_format_summary_mentions_the_decision_and_the_criterion(tmp_path, dataset):
    root = build_run(tmp_path, dataset, noisy, flipper)
    lines = []
    jr.execute("r1", root, out=lines.append)
    text = lines[0]
    assert "Critério de decisão (fixado antes dos números)" in text and "NÃO CONFIÁVEL" in text
    assert "Concordância entre os juízes (sem média de notas)" in text and "Autopreferência" in text
    assert "viés de posição" in text and "Escopo: escopo de teste" in text


def test_report_errors_are_clear(tmp_path, dataset, capsys):
    with pytest.raises(ScoreError, match="run não encontrado"):
        jr.execute("nao-existe", tmp_path)
    root = build_run(tmp_path, dataset, noisy, flipper)
    (root / "r1" / "scores.jsonl").unlink()
    with pytest.raises(ScoreError, match="evalkit.score"):
        jr.execute("r1", root)
    assert jr.main(["--run-id", "nao-existe", "--results-root", str(root)]) == 2
    assert "erro:" in capsys.readouterr().err
    empty = tmp_path / "vazio" / "r1"
    empty.mkdir(parents=True)
    (empty / "ollama_qwen.jsonl").write_text(json.dumps({"prompt_id": "extraction-01"}) + "\n", encoding="utf-8")
    (empty / "scores.jsonl").write_text("", encoding="utf-8")
    with pytest.raises(ScoreError, match="nenhum julgamento"):
        jr.execute("r1", tmp_path / "vazio")


def test_report_cli_main_returns_zero_and_never_rewrites_the_judgments(tmp_path, dataset, capsys):
    root = build_run(tmp_path, dataset, noisy, flipper)
    before = {p.name: p.read_bytes() for p in (root / "r1" / "judge").glob("*.jsonl")}
    assert jr.main(["--run-id", "r1", "--results-root", str(root)]) == 0
    assert "Juiz local" in capsys.readouterr().out
    assert before == {p.name: p.read_bytes() for p in (root / "r1" / "judge").glob("*.jsonl")}
