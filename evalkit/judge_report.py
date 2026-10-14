"""Relatório do juiz local (Marco 11): o juiz concorda com os scorers? Quanto confiar nele?

    python -m evalkit.judge_report --run-id <id>

Lê `eval/results/<id>/judge/<juiz>.jsonl` (de `evalkit.judge`), `scores.jsonl` e os resultados do runner e grava
`judge_summary.json` e `judge_review.md` (16 itens para revisão humana) na pasta do run (ignorados pelo git).
Sem rede, sem Ollama, sem Anthropic.

O juiz NÃO é tratado como verdade. A calibração compara o veredito dele (nota geral >= 4 = aprovado) com o
`passed` do scorer determinístico nas tarefas VERIFICÁVEIS, sem que ele veja o `check`. O critério de decisão
(kappa >= 0,6 e acurácia >= 0,80, por juiz) está em `evalkit.judge_stats` e foi fixado antes de ver os números.
Abaixo dele, as notas do juiz ficam marcadas como "não confiáveis" e a summarization segue sendo avaliada só
pelo scorer determinístico e pela revisão humana.

Autopreferência: quanto cada juiz favorece as respostas do PRÓPRIO modelo em relação ao outro juiz (nota média
pontual e taxa de vitória no pareado). Com só dois modelos e dois juízes isso é UM contraste (o pareado dá o
mesmo número para os dois juízes por construção): ele mostra que os juízes divergem na direção de favorecer o
próprio modelo, mas não diz sozinho qual deles está enviesado. Parte da diferença pode ser real (o Qwen acerta
mais no conjunto verificável); por isso há também a taxa de aprovações indevidas (scorer reprovou, juiz
aprovou) em respostas próprias contra respostas do outro modelo.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import random
import sys
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

from evalkit import judge_stats as js
from evalkit.judge import JUDGE_DIRNAME
from evalkit.schema import EvalPrompt
from evalkit.score import DEFAULT_SEED, RESULTS_ROOT, ScoreError, load_run, pick_pair
from evalkit.scorers import as_text
from evalkit.validate_dataset import PROMPTS_DIR, DatasetError, load_dataset

JUDGE_ORDER = ("ollama_qwen", "ollama_llama")
REVIEW_VERIFIABLE_AGREE = 4
REVIEW_VERIFIABLE_DISAGREE = 4
REVIEW_SUMMARIZATION = 8
TRUSTED_LABEL = "uso permitido nas tarefas abertas, com ressalva"
UNTRUSTED_LABEL = "NÃO CONFIÁVEL: não usar as notas do juiz nas tarefas abertas"


def r4(x: float | None) -> float | None:
    return None if x is None else round(x, 4)


def pair_ci(ci: tuple[float, float] | None) -> list[float] | None:
    return None if ci is None else [round(ci[0], 4), round(ci[1], 4)]


# --------------------------------------------------------------------------- leitura (somente leitura)
def load_judgments(run_dir: Path) -> dict[str, list[dict[str, Any]]]:
    """Julgamentos por juiz (`judge/<juiz>.jsonl`). Linhas inválidas são ignoradas; nada é reescrito."""
    out: dict[str, list[dict[str, Any]]] = {}
    jdir = run_dir / JUDGE_DIRNAME
    for path in sorted(jdir.glob("*.jsonl")) if jdir.is_dir() else []:
        recs = []
        for line in path.read_text(encoding="utf-8").splitlines():
            with contextlib.suppress(ValueError):
                rec = json.loads(line)
                if isinstance(rec, dict) and "key" in rec:
                    recs.append(rec)
        out[path.stem] = recs
    return out


def load_json(path: Path) -> dict[str, Any] | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def load_scores(run_dir: Path) -> list[dict[str, Any]]:
    path = run_dir / "scores.jsonl"
    if not path.exists():
        raise ScoreError(f"scores.jsonl não encontrado em {run_dir}: rode antes python -m evalkit.score --run-id ...")
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        with contextlib.suppress(ValueError):
            rec = json.loads(line)
            if isinstance(rec, dict) and "prompt_id" in rec:
                rows.append(rec)
    return rows


def ordered_judges(names: Sequence[str]) -> list[str]:
    return [j for j in JUDGE_ORDER if j in names] + sorted(n for n in names if n not in JUDGE_ORDER)


# --------------------------------------------------------------------------- calibração
def pointwise_rows(
    recs: list[dict[str, Any]], prompts: dict[str, EvalPrompt], scores: dict[tuple[str, str], dict[str, Any]],
    texts: dict[tuple[str, str], str],
) -> list[dict[str, Any]]:
    """Um registro por julgamento pontual, já juntado ao score do scorer determinístico."""
    rows = []
    for rec in recs:
        if rec.get("kind") != "pointwise":
            continue
        pid, target = rec["prompt_id"], rec["target"]
        prompt, sc = prompts.get(pid), scores.get((pid, target))
        if prompt is None or sc is None:
            continue
        rows.append({
            "prompt_id": pid, "target": target, "task_type": prompt.task_type, "verifiable": prompt.check is not None,
            "judge_error": bool(rec.get("judge_error")), "overall": rec.get("overall"),
            "scores": rec.get("scores") or {}, "reason": rec.get("reason") or "",
            "scorer_passed": bool(sc["passed"]), "scorer_score": sc["score"], "scorer_errored": bool(sc["errored"]),
            "scorer_details": sc.get("details") or {}, "chars": len(texts.get((target, pid), "")),
        })
    return rows


def usable(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [r for r in rows if not r["judge_error"] and not r["scorer_errored"]]


def calibration_block(rows: list[dict[str, Any]], seed: int) -> dict[str, Any]:
    ok = usable(rows)
    judge = [js.approved(r["overall"]) for r in ok]
    truth = [r["scorer_passed"] for r in ok]
    m = js.binary_metrics(judge, truth)
    wil = js.accuracy_ci(m["tp"] + m["tn"], m["n"])
    block: dict[str, Any] = {
        "n": m["n"], "judge_errors": sum(r["judge_error"] for r in rows),
        "confusion": {k: m[k] for k in ("tp", "fp", "fn", "tn")},
        "accuracy": r4(m["accuracy"]), "accuracy_ci95": pair_ci(wil),
        "precision": r4(m["precision"]), "recall": r4(m["recall"]), "kappa": r4(m["kappa"]),
        "kappa_ci95": pair_ci(js.bootstrap_kappa_ci(judge, truth, seed=seed)),
        "judge_approval_rate": r4(sum(judge) / len(judge)) if judge else None,
        "scorer_pass_rate": r4(sum(truth) / len(truth)) if truth else None,
    }
    if m["kappa"] is None and m["n"]:
        block["kappa_note"] = "indefinido (um dos lados usou uma única categoria)"
    return block


def median(values: list[float]) -> float | None:
    if not values:
        return None
    s = sorted(values)
    mid = len(s) // 2
    return float(s[mid]) if len(s) % 2 else (s[mid - 1] + s[mid]) / 2


def error_analysis(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Onde e por que o juiz erra no conjunto verificável: quais respostas, que nota, e o tamanho delas."""
    ok = usable(rows)
    cells: dict[str, list[dict[str, Any]]] = {"true_approve": [], "false_approve": [], "true_reject": [],
                                              "false_reject": []}
    for r in ok:
        j, t = js.approved(r["overall"]), r["scorer_passed"]
        cells[("true_" if j == t else "false_") + ("approve" if j else "reject")].append(r)

    def brief(r: dict[str, Any]) -> dict[str, Any]:
        return {"prompt_id": r["prompt_id"], "target": r["target"], "overall": r["overall"],
                "reason": r["reason"][:200], "scorer_score": r["scorer_score"], "chars": r["chars"]}

    failed = [r for r in ok if not r["scorer_passed"]]
    dist: dict[str, int] = {}
    for r in failed:
        dist[str(r["overall"])] = dist.get(str(r["overall"]), 0) + 1
    return {
        "median_chars": {k: median([float(r["chars"]) for r in v]) for k, v in cells.items()},
        "counts": {k: len(v) for k, v in cells.items()},
        "overall_when_scorer_failed": dict(sorted(dist.items())),
        "false_approvals": [brief(r) for r in cells["false_approve"]],
        "false_rejections": [brief(r) for r in cells["false_reject"]],
    }


def criterion_dict() -> dict[str, Any]:
    return {"kappa_min": js.KAPPA_MIN, "accuracy_min": js.ACCURACY_MIN, "approve_min_overall": js.APPROVE_MIN}


def decision(block: dict[str, Any]) -> dict[str, Any]:
    ok = js.meets_criterion(block["kappa"], block["accuracy"])
    return {
        "criterion": criterion_dict(),
        "kappa": block["kappa"], "accuracy": block["accuracy"], "meets_criterion": ok,
        "label": TRUSTED_LABEL if ok else UNTRUSTED_LABEL,
    }


# --------------------------------------------------------------------------- pareado
def pairwise_outcomes(recs: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """prompt_id -> resultado das duas rodadas (ver `judge_stats.pairwise_outcome`)."""
    rounds: dict[str, dict[int, str | None]] = {}
    for rec in recs:
        if rec.get("kind") == "pairwise":
            rounds.setdefault(rec["prompt_id"], {})[int(rec["round"])] = rec.get("winner_model")
    return {
        pid: js.pairwise_outcome(r.get(1), r.get(2)) | {"r1": r.get(1), "r2": r.get(2)} for pid, r in rounds.items()
    }


def pairwise_block(recs: list[dict[str, Any]], pair: tuple[str, str] | None) -> dict[str, Any]:
    outcomes = pairwise_outcomes(recs)
    valid = {pid: o for pid, o in outcomes.items() if o["valid"]}
    finals = [o["final"] for o in valid.values()]
    rounds = [r for r in recs if r.get("kind") == "pairwise"]
    decisive = [r for r in rounds if r.get("winner") in ("A", "B")]
    block: dict[str, Any] = {
        "prompts": len(outcomes), "valid": len(valid),
        "judge_errors": sum(bool(r.get("judge_error")) for r in rounds),
        **{k: (r4(v) if isinstance(v, float) else v) for k, v in js.position_bias(list(outcomes.values())).items()},
        "first_position_pick_rate": r4(sum(r["winner"] == "A" for r in decisive) / len(decisive)) if decisive else None,
        "final_preference": {},
    }
    if pair:
        block["final_preference"] = {
            pair[0]: sum(f == pair[0] for f in finals), pair[1]: sum(f == pair[1] for f in finals),
            "tie": sum(f == js.TIE for f in finals),
        }
        block["win_rate"] = {m: r4(js.win_rate(finals, m)) for m in pair}
    return block


# --------------------------------------------------------------------------- tarefas abertas
def open_tasks_block(rows: list[dict[str, Any]], label: str) -> dict[str, Any]:
    """Summarization: nota do juiz contra o score determinístico (Spearman; só 16 respostas)."""
    ok = [r for r in usable(rows) if r["task_type"] == "summarization"]

    def corr(sub: list[dict[str, Any]]) -> dict[str, Any]:
        rho = js.spearman([r["overall"] for r in sub], [r["scorer_score"] for r in sub])
        return {"n": len(sub), "spearman": r4(rho),
                "mean_judge_overall": r4(js.mean([r["overall"] for r in sub])),
                "mean_scorer_score": r4(js.mean([r["scorer_score"] for r in sub]))}

    by_target = {t: corr([r for r in ok if r["target"] == t]) for t in sorted({r["target"] for r in ok})}
    return {
        "reliability": label, "judge_errors": sum(r["judge_error"] for r in rows if r["task_type"] == "summarization"),
        "pooled": corr(ok), "by_target": by_target,
        "warning": "só 16 respostas (8 prompts x 2 modelos): a correlação é indicativa, com intervalo muito largo",
    }


def instruction_extra(rows: list[dict[str, Any]], seed: int) -> dict[str, Any]:
    """Análise extra: o scorer de `instruction` já confere as restrições por regra (palavras, itens, linhas,
    frases); aqui só se mede o quanto o juiz concorda com ele."""
    sub = [r for r in rows if r["task_type"] == "instruction"]
    ok = usable(sub)
    return {
        "note": "o scorer já cobre as restrições verificáveis; o juiz é medido contra ele, não o substitui",
        "calibration": calibration_block(sub, seed),
        "spearman_overall_vs_scorer": r4(js.spearman([r["overall"] for r in ok], [r["scorer_score"] for r in ok])),
    }


# --------------------------------------------------------------------------- juízes cruzados
def pointwise_index(rows: list[dict[str, Any]]) -> dict[tuple[str, str], dict[str, Any]]:
    return {(r["prompt_id"], r["target"]): r for r in rows if not r["judge_error"]}


def cross_judge(rows_by_judge: dict[str, list[dict[str, Any]]], recs_by_judge: dict[str, list[dict[str, Any]]],
                names: tuple[str, str]) -> dict[str, Any]:
    a_name, b_name = names
    ia, ib = pointwise_index(rows_by_judge[a_name]), pointwise_index(rows_by_judge[b_name])
    keys = sorted(ia.keys() & ib.keys())
    oa, ob = [ia[k]["overall"] for k in keys], [ib[k]["overall"] for k in keys]
    va, vb = [js.approved(x) for x in oa], [js.approved(x) for x in ob]
    point = {
        "n": len(keys),
        "approval_agreement": r4(sum(x == y for x, y in zip(va, vb, strict=True)) / len(keys)) if keys else None,
        "approval_kappa": r4(js.cohen_kappa(va, vb)),
        "exact_overall_agreement": r4(sum(x == y for x, y in zip(oa, ob, strict=True)) / len(keys)) if keys else None,
        "within_one_point": r4(sum(abs(x - y) <= 1 for x, y in zip(oa, ob, strict=True)) / len(keys)) if keys else None,
        "spearman_overall": r4(js.spearman(oa, ob)),
        "mean_overall": {a_name: r4(js.mean(oa)), b_name: r4(js.mean(ob))},
    }
    pa = {pid: o for pid, o in pairwise_outcomes(recs_by_judge[a_name]).items() if o["valid"]}
    pb = {pid: o for pid, o in pairwise_outcomes(recs_by_judge[b_name]).items() if o["valid"]}
    pids = sorted(pa.keys() & pb.keys())
    fa, fb = [pa[p]["final"] for p in pids], [pb[p]["final"] for p in pids]
    pair = {
        "n": len(pids),
        "preference_agreement": r4(sum(x == y for x, y in zip(fa, fb, strict=True)) / len(pids)) if pids else None,
        "preference_kappa": r4(js.cohen_kappa(fa, fb)),
    }
    return {"pointwise": point, "pairwise": pair,
            "note": "as notas dos juízes NÃO são promediadas; cada juiz é reportado separadamente"}


def self_preference(rows_by_judge: dict[str, list[dict[str, Any]]], recs_by_judge: dict[str, list[dict[str, Any]]],
                    names: tuple[str, str]) -> dict[str, Any]:
    """Para cada juiz J (cujo modelo é o provedor de mesmo nome): quanto J favorece as respostas do próprio
    modelo em relação ao outro juiz K, nas mesmas respostas."""
    idx = {j: pointwise_index(rows_by_judge[j]) for j in names}
    outcomes = {j: {p: o for p, o in pairwise_outcomes(recs_by_judge[j]).items() if o["valid"]} for j in names}
    common_pids = sorted(outcomes[names[0]].keys() & outcomes[names[1]].keys())
    out: dict[str, Any] = {}
    for j, k in (names, names[::-1]):
        own = sorted(key for key in idx[j].keys() & idx[k].keys() if key[1] == j)
        other = sorted(key for key in idx[j].keys() & idx[k].keys() if key[1] == k)
        own_gap = js.gap(js.mean([idx[j][x]["overall"] for x in own]), js.mean([idx[k][x]["overall"] for x in own]))
        other_gap = js.gap(js.mean([idx[j][x]["overall"] for x in other]),
                           js.mean([idx[k][x]["overall"] for x in other]))
        wr_j = js.win_rate([outcomes[j][p]["final"] for p in common_pids], j)
        wr_k = js.win_rate([outcomes[k][p]["final"] for p in common_pids], j)

        def false_approval(target: str, judge: str = j) -> dict[str, Any]:
            failed = [r for r in usable(rows_by_judge[judge]) if r["verifiable"] and r["target"] == target
                      and not r["scorer_passed"]]
            n = sum(js.approved(r["overall"]) for r in failed)
            return {"scorer_failed": len(failed), "judge_approved": n, "rate": r4(n / len(failed)) if failed else None}

        out[j] = {
            "pointwise_own_gap": r4(own_gap),
            "pointwise_leniency_gap_on_other_model": r4(other_gap),
            "pointwise_net": r4(js.gap(own_gap, other_gap)),
            "pairwise_win_rate_of_own_by_this_judge": r4(wr_j),
            "pairwise_win_rate_of_own_by_other_judge": r4(wr_k),
            "pairwise_own_gap": r4(js.gap(wr_j, wr_k)),
            "false_approvals_on_own_answers": false_approval(j),
            "false_approvals_on_other_answers": false_approval(k),
        }
    out["note"] = (
        "pointwise_own_gap = nota média que o juiz dá às respostas do próprio modelo menos a que o outro juiz dá "
        "às mesmas respostas; a folga geral (leniency) está em pointwise_leniency_gap_on_other_model e "
        "pointwise_net a desconta. Com dois modelos e dois juízes, pairwise_own_gap e pointwise_net são o MESMO "
        "contraste para os dois juízes por construção: mostram que os juízes divergem favorecendo o próprio "
        "modelo, mas não dizem qual deles é o enviesado. Parte do efeito pode ser real (o Qwen acerta mais)."
    )
    return out


# --------------------------------------------------------------------------- montagem
def counts_block(recs: list[dict[str, Any]]) -> dict[str, Any]:
    out = {}
    for kind in ("pointwise", "pairwise"):
        sub = [r for r in recs if r.get("kind") == kind]
        out[kind] = {"total": len(sub), "judge_errors": sum(bool(r.get("judge_error")) for r in sub),
                     "retried": sum(r.get("attempts", 1) > 1 for r in sub),
                     "recovered_from_truncation": sum(bool(r.get("recovered")) for r in sub)}
    return out


def build_summary(
    *, run_id: str, prompts: dict[str, EvalPrompt], results: dict[str, list[dict[str, Any]]],
    score_rows: list[dict[str, Any]], recs_by_judge: dict[str, list[dict[str, Any]]],
    metas: dict[str, dict[str, Any] | None], seed: int = DEFAULT_SEED,
) -> dict[str, Any]:
    scores = {(r["prompt_id"], r["provider"]): r for r in score_rows}
    texts = {(p, rec["prompt_id"]): as_text(rec.get("response")) for p, recs in results.items() for rec in recs}
    pair = pick_pair(sorted(results))
    judges = ordered_judges(list(recs_by_judge))
    rows_by_judge = {j: pointwise_rows(recs_by_judge[j], prompts, scores, texts) for j in judges}
    summary: dict[str, Any] = {
        "run_id": run_id, "seed": seed, "pair": list(pair) if pair else None,
        "scope": (metas.get(judges[0]) or {}).get("scope") if judges else None,
        "criterion": {**criterion_dict(),
                      "set": "todas as categorias com check (extraction, classification, reasoning, instruction, "
                             "tool_use)"},
        "judges": {},
    }
    for j in judges:
        rows = rows_by_judge[j]
        verifiable = [r for r in rows if r["verifiable"]]
        cal = calibration_block(verifiable, seed)
        dec = decision(cal)
        meta = metas.get(j) or {}
        summary["judges"][j] = {
            "model": meta.get("model"), "judge_prompt_hash": meta.get("judge_prompt_hash"),
            "judge_prompt_version": meta.get("judge_prompt_version"), "counts": counts_block(recs_by_judge[j]),
            "calibration": {
                "verifiable": cal,
                "by_task_type": {t: calibration_block([r for r in verifiable if r["task_type"] == t], seed)
                                 for t in sorted({r["task_type"] for r in verifiable})},
                "decision": dec, "error_analysis": error_analysis(verifiable),
            },
            "pairwise": pairwise_block(recs_by_judge[j], pair),
            "open_tasks": {"summarization": open_tasks_block(rows, dec["label"]),
                           "instruction_extra": instruction_extra(rows, seed)},
        }
    if pair and set(pair) <= set(judges) and len(pair) == 2:
        names = (pair[0], pair[1])
        summary["cross_judge"] = cross_judge(rows_by_judge, recs_by_judge, names)
        summary["self_preference"] = self_preference(rows_by_judge, recs_by_judge, names)
    else:
        summary["cross_judge"] = None
        summary["self_preference"] = None
    return summary


# --------------------------------------------------------------------------- texto
def pct(x: float | None) -> str:
    return "-" if x is None else f"{x * 100:.0f}%"


def num(x: float | None, nd: int = 2) -> str:
    return "-" if x is None else f"{x:.{nd}f}"


def format_summary(s: dict[str, Any]) -> str:
    lines = [f"Juiz local | run {s['run_id']} | semente {s['seed']}",
             f"Critério de decisão (fixado antes dos números): kappa >= {js.KAPPA_MIN} e acurácia >= "
             f"{js.ACCURACY_MIN:.2f} no conjunto verificável; aprovado = nota geral >= {js.APPROVE_MIN}."]
    for j, d in s["judges"].items():
        cal, c = d["calibration"], d["counts"]
        lines += ["", f"=== Juiz {j} ({d['model']}) | prompt do juiz {str(d['judge_prompt_hash'])[:12]} ===",
                  f"julgamentos: pontual {c['pointwise']['total']} (judge_error {c['pointwise']['judge_errors']}), "
                  f"pareado {c['pairwise']['total']} (judge_error {c['pairwise']['judge_errors']})",
                  "Calibração contra o scorer (aprovação):",
                  f"  {'categoria':<15} {'n':>3} {'acur.':>6} {'prec.':>6} {'rev.':>6} {'kappa':>6}  "
                  "matriz (tp fp fn tn)"]
        for label, b in [("(verificável)", cal["verifiable"]), *cal["by_task_type"].items()]:
            cf = b["confusion"]
            lines.append(f"  {label:<15} {b['n']:>3} {pct(b['accuracy']):>6} {pct(b['precision']):>6} "
                         f"{pct(b['recall']):>6} {num(b['kappa']):>6}  {cf['tp']} {cf['fp']} {cf['fn']} {cf['tn']}")
        v = cal["verifiable"]
        lines.append(f"  IC95 acurácia {v['accuracy_ci95']} | IC95 kappa (bootstrap) {v['kappa_ci95']}")
        lines.append(f"  DECISÃO: {cal['decision']['label']} (kappa {num(v['kappa'])}, acurácia {pct(v['accuracy'])})")
        p = d["pairwise"]
        lines += [f"Pareado ({p['valid']} prompts válidos de {p['prompts']}): viés de posição "
                  f"{pct(p['position_bias_index'])} ({p['flipped']} trocas de lado), inconsistência total "
                  f"{pct(p['order_inconsistency'])}, escolhe a posição A em {pct(p['first_position_pick_rate'])} "
                  f"das rodadas decisivas",
                  f"  preferência final: {p['final_preference']} | taxa de vitória: {p.get('win_rate')}"]
        sm = d["open_tasks"]["summarization"]
        lines.append(f"Summarization [{sm['reliability']}]: Spearman nota x score = {num(sm['pooled']['spearman'])} "
                     f"(n={sm['pooled']['n']}); por modelo: "
                     + ", ".join(f"{t} {num(b['spearman'])} (n={b['n']})" for t, b in sm["by_target"].items()))
        ie = d["open_tasks"]["instruction_extra"]
        lines.append(f"Instruction (extra): acurácia {pct(ie['calibration']['accuracy'])}, kappa "
                     f"{num(ie['calibration']['kappa'])}, Spearman nota x score "
                     f"{num(ie['spearman_overall_vs_scorer'])}")
        ea = d["calibration"]["error_analysis"]
        lines.append(f"Erros do juiz: aprovações indevidas {ea['counts']['false_approve']}, reprovações indevidas "
                     f"{ea['counts']['false_reject']}; mediana de caracteres {ea['median_chars']}")
    cj = s.get("cross_judge")
    if cj:
        p, w = cj["pointwise"], cj["pairwise"]
        lines += ["", "=== Concordância entre os juízes (sem média de notas) ===",
                  f"pontual (n={p['n']}): aprovação concorda em {pct(p['approval_agreement'])} (kappa "
                  f"{num(p['approval_kappa'])}), nota idêntica em {pct(p['exact_overall_agreement'])}, "
                  f"diferença <= 1 em {pct(p['within_one_point'])}, Spearman {num(p['spearman_overall'])}",
                  f"pareado (n={w['n']}): preferência final concorda em {pct(w['preference_agreement'])} "
                  f"(kappa {num(w['preference_kappa'])})"]
    sp = s.get("self_preference")
    if sp:
        lines += ["", "=== Autopreferência (próprio modelo, em relação ao outro juiz) ==="]
        for j in (k for k in sp if k != "note"):
            b = sp[j]
            own, oth = b["false_approvals_on_own_answers"], b["false_approvals_on_other_answers"]
            lines.append(
                f"{j}: nota média nas respostas do próprio modelo menos a do outro juiz = "
                f"{num(b['pointwise_own_gap'])} (folga geral {num(b['pointwise_leniency_gap_on_other_model'])}, "
                f"líquido {num(b['pointwise_net'])}); vitória do próprio no pareado "
                f"{pct(b['pairwise_win_rate_of_own_by_this_judge'])} contra "
                f"{pct(b['pairwise_win_rate_of_own_by_other_judge'])} pelo outro juiz "
                f"(dif. {num(b['pairwise_own_gap'])}); aprovações indevidas em respostas próprias "
                f"{own['judge_approved']}/{own['scorer_failed']}, nas do outro "
                f"{oth['judge_approved']}/{oth['scorer_failed']}")
        lines.append(sp["note"])
    if s.get("scope"):
        lines += ["", f"Escopo: {s['scope']}"]
    return "\n".join(lines)


# --------------------------------------------------------------------------- revisão humana
def stratified_pick(rng: random.Random, candidates: list[dict[str, Any]], k: int, *, key: str = "task_type",
                    already: Sequence[dict[str, Any]] = ()) -> list[dict[str, Any]]:
    """`k` itens espalhados entre as categorias (rodízio em ordem sorteada), sem repetir os de `already`."""
    taken = {(r["prompt_id"], r["target"]) for r in already}
    pools: dict[str, list[dict[str, Any]]] = {}
    for r in sorted(candidates, key=lambda r: (r["prompt_id"], r["target"])):
        if (r["prompt_id"], r["target"]) not in taken:
            pools.setdefault(r[key], []).append(r)
    for pool in pools.values():
        rng.shuffle(pool)
    order = sorted(pools)
    rng.shuffle(order)
    picked: list[dict[str, Any]] = []
    while len(picked) < k and any(pools.values()):
        for cat in order:
            if pools[cat] and len(picked) < k:
                picked.append(pools[cat].pop())
    return picked


def pick_review_items(rows_by_judge: dict[str, list[dict[str, Any]]], seed: int) -> dict[str, list[dict[str, Any]]]:
    """16 itens por semente fixa: 8 verificáveis (4 em que TODOS os juízes concordam com o scorer e 4 em que algum
    discorda, incluindo aprovações indevidas de respostas do Qwen) e 8 de summarization (metade de cada modelo)."""
    rng = random.Random(seed)
    judges = list(rows_by_judge)
    verdicts: dict[tuple[str, str], dict[str, Any]] = {}
    for j in judges:
        for r in usable(rows_by_judge[j]):
            v = verdicts.setdefault((r["prompt_id"], r["target"]), {
                "prompt_id": r["prompt_id"], "target": r["target"], "task_type": r["task_type"],
                "verifiable": r["verifiable"], "scorer_passed": r["scorer_passed"], "judge_ok": {}})
            v["judge_ok"][j] = js.approved(r["overall"]) == r["scorer_passed"]
    verifiable = [v for v in verdicts.values() if v["verifiable"] and v["judge_ok"]]
    agree = [v for v in verifiable if all(v["judge_ok"].values())]
    disagree = [v for v in verifiable if not all(v["judge_ok"].values())]
    qwen_false_approvals = [v for v in disagree if v["target"] == "ollama_qwen" and not v["scorer_passed"]]
    picked_dis = stratified_pick(rng, qwen_false_approvals, min(2, REVIEW_VERIFIABLE_DISAGREE))
    picked_dis += stratified_pick(rng, disagree, REVIEW_VERIFIABLE_DISAGREE - len(picked_dis), already=picked_dis)
    picked_agree = stratified_pick(rng, agree, REVIEW_VERIFIABLE_AGREE)
    summ = [v for v in verdicts.values() if v["task_type"] == "summarization"]
    targets = sorted({v["target"] for v in summ})
    picked_sum: list[dict[str, Any]] = []
    for t in targets:
        pool = sorted((v for v in summ if v["target"] == t), key=lambda v: v["prompt_id"])
        picked_sum += rng.sample(pool, min(REVIEW_SUMMARIZATION // max(len(targets), 1), len(pool)))
    picked_sum.sort(key=lambda v: (v["prompt_id"], v["target"]))
    return {"agree": picked_agree, "disagree": picked_dis, "summarization": picked_sum}


def fenced(text: str) -> str:
    """Texto em bloco de código sem que a resposta consiga fechar o bloco."""
    return "````text\n" + text.replace("````", "''''") + "\n````"


def build_review(
    *, run_id: str, prompts: dict[str, EvalPrompt], results: dict[str, list[dict[str, Any]]],
    score_rows: list[dict[str, Any]], recs_by_judge: dict[str, list[dict[str, Any]]],
    rows_by_judge: dict[str, list[dict[str, Any]]], seed: int = DEFAULT_SEED,
) -> str:
    picks = pick_review_items(rows_by_judge, seed)
    scores = {(r["prompt_id"], r["provider"]): r for r in score_rows}
    texts = {(p, rec["prompt_id"]): as_text(rec.get("response")) for p, recs in results.items() for rec in recs}
    pair = pick_pair(sorted(results))
    point = {j: pointwise_index(rows) for j, rows in rows_by_judge.items()}
    pw = {j: pairwise_outcomes(recs) for j, recs in recs_by_judge.items()}
    sections = [
        ("Tarefas verificáveis: todos os juízes concordam com o scorer", picks["agree"]),
        ("Tarefas verificáveis: algum juiz discorda do scorer", picks["disagree"]),
        ("Summarization (sem `check`: o scorer é a regra composta)", picks["summarization"]),
    ]
    out = [
        f"# Revisão humana do juiz | run `{run_id}`", "",
        f"Semente {seed}. 16 itens: 8 verificáveis (4 em que os juízes concordam com o scorer, 4 em que algum "
        "discorda, incluindo aprovações indevidas de respostas do Qwen quando existem) e 8 de summarization "
        "(4 de cada modelo). Para cada item: enunciado, resposta, nota e motivo de cada juiz, score e `details` do "
        "scorer determinístico. Marque no fim de cada item o que VOCÊ acha; isso calibra o juiz e o scorer.", "",
        "Lembrete: o juiz nunca viu o `check` nem o score. O scorer mede por regra; para summarization ele é uma "
        "composição de cobertura de pontos-chave, similaridade e tamanho.", ""]
    n = 0
    for title, items in sections:
        out += [f"## {title}", ""]
        for item in items:
            n += 1
            pid, target = item["prompt_id"], item["target"]
            prompt = prompts[pid]
            sc = scores.get((pid, target), {})
            kind = prompt.check.type if prompt.check else "summarization"
            out += [f"### {n}. {pid} | resposta de {target} | {kind}", "",
                    "**Enunciado:**", fenced(prompt.messages[-1].content), "",
                    f"**Resposta de {target}:**", fenced(texts.get((target, pid), "")), ""]
            if prompt.reference_answer:
                out += ["**Referência humana:**", fenced(prompt.reference_answer), ""]
            if item["task_type"] == "summarization" and pair:
                other = pair[1] if target == pair[0] else pair[0]
                out += [f"**Resposta do outro modelo ({other}):**", fenced(texts.get((other, pid), "")), ""]
            for j, idx in point.items():
                r = idx.get((pid, target))
                if r is None:
                    out.append(f"- Juiz `{j}`: sem nota (judge_error ou não julgado).")
                    continue
                verdict = "aprova" if js.approved(r["overall"]) else "reprova"
                out.append(f"- Juiz `{j}`: nota geral **{r['overall']}** ({verdict}), critérios {r['scores']}. "
                           f"Motivo: {r['reason'] or '-'}")
            out.append(f"- Scorer: score **{sc.get('score')}**, passed **{sc.get('passed')}**")
            out.append("- `details`: `" + json.dumps(sc.get("details", {}), ensure_ascii=False) + "`")
            if item["task_type"] == "summarization":
                for j, outcomes in pw.items():
                    o = outcomes.get(pid)
                    if o and o["valid"]:
                        out.append(f"- Pareado, juiz `{j}`: rodada 1 = {o['r1']}, rodada 2 = {o['r2']}, "
                                   f"final = {o['final']}")
            out += ["", "Seu veredito: [ ] correta  [ ] parcial  [ ] incorreta", "",
                    "Concorda com o juiz? [ ] sim [ ] não | Concorda com o scorer? [ ] sim [ ] não", "", "---", ""]
    return "\n".join(out)


# --------------------------------------------------------------------------- CLI
def execute(run_id: str, results_root: Path = RESULTS_ROOT, dataset_dir: Path = PROMPTS_DIR, seed: int = DEFAULT_SEED,
            out: Callable[[str], None] = print) -> dict[str, Any]:
    run_dir = results_root / run_id
    results = load_run(run_dir)
    try:
        prompts = load_dataset(dataset_dir).by_id()
    except DatasetError as e:
        raise ScoreError("dataset inválido (rode python -m evalkit.validate_dataset)") from e
    score_rows = load_scores(run_dir)
    recs_by_judge = load_judgments(run_dir)
    if not recs_by_judge:
        raise ScoreError(f"nenhum julgamento em {run_dir / JUDGE_DIRNAME}: rode python -m evalkit.judge --run-id "
                         f"{run_id} --judge ollama_qwen (e depois ollama_llama)")
    metas = {j: load_json(run_dir / JUDGE_DIRNAME / f"{j}.meta.json") for j in recs_by_judge}
    summary = build_summary(run_id=run_id, prompts=prompts, results=results, score_rows=score_rows,
                            recs_by_judge=recs_by_judge, metas=metas, seed=seed)
    scores = {(r["prompt_id"], r["provider"]): r for r in score_rows}
    texts = {(p, rec["prompt_id"]): as_text(rec.get("response")) for p, recs in results.items() for rec in recs}
    rows_by_judge = {j: pointwise_rows(recs, prompts, scores, texts) for j, recs in recs_by_judge.items()}
    review = build_review(run_id=run_id, prompts=prompts, results=results, score_rows=score_rows,
                          recs_by_judge=recs_by_judge, rows_by_judge=rows_by_judge, seed=seed)
    summary_text = json.dumps(summary, ensure_ascii=False, indent=2) + "\n"
    (run_dir / "judge_summary.json").write_text(summary_text, encoding="utf-8")
    (run_dir / "judge_review.md").write_text(review + "\n", encoding="utf-8")
    out(format_summary(summary))
    return summary


def parse_args(argv: list[str] | None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Relatório de calibração do juiz local (sem rede, sem Anthropic).")
    p.add_argument("--run-id", required=True)
    p.add_argument("--seed", type=int, default=DEFAULT_SEED, help="semente do bootstrap e da amostra de revisão")
    p.add_argument("--results-root", type=Path, default=RESULTS_ROOT, help=argparse.SUPPRESS)
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    for stream in (sys.stdout, sys.stderr):
        with contextlib.suppress(AttributeError, ValueError):
            stream.reconfigure(encoding="utf-8", errors="replace")
    try:
        execute(args.run_id, args.results_root, seed=args.seed)
        return 0
    except ScoreError as e:
        print(f"erro: {e}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
