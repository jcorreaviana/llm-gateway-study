"""Relatório final da Fase 3 (Marco 12): Qwen contra Llama, em markdown e CSV, só com agregados e ids de prompts.

    python -m evalkit.report --run-id <id> [--out-dir reports]

Lê, na pasta `eval/results/<id>/`: `summary.json`, `scores.jsonl`, `meta.json` e, se existirem,
`judge_summary.json` e `human_verdicts.json`. Grava `<out-dir>/fase3-qwen-vs-llama.md` e `.csv`. Sem rede, sem
Ollama, sem Anthropic.

O relatório é PÚBLICO. Por isso:
- nenhum texto de resposta de modelo (nem motivo de juiz) chega ao arquivo: as linhas de `scores.jsonl` passam por
  `sanitize_score_row` (lista de campos permitidos) e do juiz só se leem números e rótulos (`judge_view`);
- antes de gravar, `find_leaks` confere que nenhuma resposta bruta do run aparece no texto gerado;
- a saída é determinística: a data vem do `meta.json` e nada depende do relógio da máquina (o modelo da GPU é
  injetado; a CLI o lê do `nvidia-smi`, se houver).
"""

from __future__ import annotations

import argparse
import csv
import io
import json
import shutil
import subprocess
import sys
from collections import Counter
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

from evalkit.score import INCONCLUSIVE, RESULTS_ROOT, ScoreError, load_run
from evalkit.scorers import as_text
from gateway.config import PROJECT_ROOT

REPORTS_DIR = PROJECT_ROOT / "reports"
REPORT_BASENAME = "fase3-qwen-vs-llama"
CATEGORY_ORDER = ("classification", "extraction", "instruction", "reasoning", "summarization", "tool_use")
SCORE_FIELDS = ("prompt_id", "provider", "task_type", "score", "passed", "errored", "latency_ms", "tokens_per_s",
                "completion_tokens")
SHORT_ANSWER_TOKENS = 10  # mediana de completion_tokens abaixo disso: tok/s dominado por overhead
MIN_LEAK_CHARS = 12  # respostas menores que isso (um número, um rótulo) não são testáveis como trecho
CSV_COLUMNS = ("provider", "task_type", "n", "errors", "mean_score", "pass_rate", "ci95_low", "ci95_high",
               "latency_p50_ms", "latency_p95_ms", "tokens_per_s_p50")
OVERALL = "all"

# Avisos obrigatórios (Seção 2; repetidos de forma curta onde são relevantes).
W_SCOPE = ("Este relatório mede o Qwen (qwen2.5:3b) contra o Llama (llama3.2:3b), via gateway, com provedor "
           "forçado. NÃO mede a perda em relação a um modelo de mercado. Nenhuma chamada à Anthropic foi feita.")
W_DATASET = ("O dataset é sintético, em PT-BR, com 8 prompts por categoria. Resultados por categoria são "
             "\"indicativo, não conclusivo\" quando o IC inclui zero ou o McNemar não confirma.")
W_REASONING = ("O enunciado de `reasoning` proíbe explicar a resposta, o que pode penalizar modelos pequenos (sem "
               "cadeia de raciocínio escrita); o tamanho desse efeito não foi medido.")
W_LATENCY = ("As latências são de modelo quente em GPU. O tok/s de respostas curtas é dominado por overhead e só é "
             "comparável nas categorias com respostas longas.")
W_SUMMARIZATION = ("Os scorers de summarization dão crédito parcial a pontos-chave compostos (exemplo: um resumo sem "
                   "os 2,4 GHz recebeu cobertura 1,0). As aprovações da summarization são um teto, não o valor exato.")
W_INSTRUCTION = ("O scorer de `instruction-07` falha por `ends_with \"!\"` por causa de aspas na resposta, e não "
                 "existe checagem explícita de \"sem aspas\".")
WARNINGS = (W_SCOPE, W_DATASET, W_REASONING, W_LATENCY, W_SUMMARIZATION, W_INSTRUCTION)


class ReportError(ScoreError):
    """Pré-requisito ausente ou relatório inseguro: mensagem clara, sem traceback."""


# --------------------------------------------------------------------------- leitura (com filtro)
def sanitize_score_row(row: dict[str, Any]) -> dict[str, Any]:
    """FILTRO de segurança: só campos numéricos e ids passam. `details` (que pode trazer valores extraídos da
    resposta, rótulos achados, campos errados) é reduzido ao booleano `format_clean`."""
    out = {k: row.get(k) for k in SCORE_FIELDS}
    flag = (row.get("details") or {}).get("format_clean")
    out["format_clean"] = flag if isinstance(flag, bool) else None
    return out


def load_json(path: Path, *, required: bool) -> dict[str, Any] | None:
    if not path.is_file():
        if required:
            raise ReportError(f"arquivo ausente: {path.name} (rode python -m evalkit.score --run-id <id>)")
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except ValueError as e:
        raise ReportError(f"{path.name} não é um JSON válido") from e


def load_score_rows(run_dir: Path) -> list[dict[str, Any]]:
    path = run_dir / "scores.jsonl"
    if not path.is_file():
        raise ReportError("scores.jsonl ausente (rode python -m evalkit.score --run-id <id>)")
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            try:
                rows.append(sanitize_score_row(json.loads(line)))
            except ValueError as e:
                raise ReportError("scores.jsonl tem uma linha inválida") from e
    return rows


def judge_view(js: dict[str, Any]) -> dict[str, Any]:
    """Só números e rótulos do `judge_summary.json`. Os motivos escritos pelos juízes (`reason`) e as listas de
    erros por prompt ficam de fora: são texto de modelo."""
    judges = {}
    for name, j in (js.get("judges") or {}).items():
        cal = j.get("calibration", {})
        judges[name] = {
            "model": j.get("model"), "prompt_version": j.get("judge_prompt_version"),
            "verifiable": cal.get("verifiable", {}),
            "by_task_type": cal.get("by_task_type", {}),
            "decision": cal.get("decision", {}),
            "pairwise": {k: v for k, v in j.get("pairwise", {}).items()
                         if k in ("prompts", "valid", "flipped", "tie_vs_pick", "position_bias_index",
                                  "order_inconsistency", "first_position_pick_rate", "final_preference",
                                  "win_rate")},
            "summarization": (j.get("open_tasks", {}).get("summarization", {}) or {}).get("pooled", {}),
            "instruction_extra": (j.get("open_tasks", {}).get("instruction_extra", {}) or {}).get("calibration", {}),
        }
    return {"criterion": js.get("criterion", {}), "judges": judges, "cross_judge": js.get("cross_judge", {}),
            "self_preference": js.get("self_preference", {})}


# --------------------------------------------------------------------------- formatação
def pct(x: float | None, nd: int = 1) -> str:
    return "n/d" if x is None else f"{x * 100:.{nd}f}%".replace(".", ",")


def num(x: float | None, nd: int = 2) -> str:
    return "n/d" if x is None else f"{x:.{nd}f}".replace(".", ",")


def ci_text(ci: Sequence[float] | None, *, as_pct: bool = True, nd: int = 2) -> str:
    if not ci:
        return "n/d"
    f = (lambda v: pct(v, 0)) if as_pct else (lambda v: num(v, nd))
    return f"{f(ci[0])} a {f(ci[1])}"


def table(header: Sequence[str], rows: Sequence[Sequence[Any]]) -> list[str]:
    out = ["| " + " | ".join(header) + " |", "|" + "|".join("---" for _ in header) + "|"]
    out += ["| " + " | ".join(str(c) for c in row) + " |" for row in rows]
    return out + [""]


def order_categories(names: Sequence[str]) -> list[str]:
    return sorted(names, key=lambda c: (CATEGORY_ORDER.index(c) if c in CATEGORY_ORDER else len(CATEGORY_ORDER), c))


def short_name(provider: str) -> str:
    return {"ollama_qwen": "Qwen", "ollama_llama": "Llama"}.get(provider, provider)


# --------------------------------------------------------------------------- CSV
def build_csv(summary: dict[str, Any], providers: Sequence[str]) -> str:
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\n")
    w.writerow(CSV_COLUMNS)

    def row(provider: str, label: str, g: dict[str, Any]) -> list[Any]:
        ci = g.get("pass_rate_ci95") or [None, None]
        vals = [g.get("mean_score"), g.get("pass_rate"), ci[0], ci[1], g.get("latency_p50_ms"),
                g.get("latency_p95_ms"), g.get("tokens_per_s_p50")]
        return [provider, label, g.get("n"), g.get("errors")] + ["" if v is None else v for v in vals]

    for p in providers:
        block = summary["providers"][p]
        w.writerow(row(p, OVERALL, block["overall"]))
        for cat in order_categories(list(block["by_task_type"])):
            w.writerow(row(p, cat, block["by_task_type"][cat]))
    return buf.getvalue()


# --------------------------------------------------------------------------- seções
def paired_status(block: dict[str, Any]) -> tuple[str, bool]:
    """(rótulo, conclusivo). O rótulo nunca nomeia vencedor quando não é conclusivo."""
    v = block.get("verdict", "")
    if v.startswith(INCONCLUSIVE):
        lo, hi = block.get("diff_ci95") or (0.0, 0.0)
        why = "IC exclui zero, McNemar não confirma" if (lo > 0 or hi < 0) else "IC inclui zero"
        return f"{INCONCLUSIVE} ({why})", False
    return "diferença detectada", True


def per_category_n(summary: dict[str, Any]) -> str:
    """Prompts por categoria lidos dos dados (\"8\", ou \"4 a 8\" se variar)."""
    sizes = sorted({g["n"] for b in summary["providers"].values() for g in b["by_task_type"].values()})
    return "n/d" if not sizes else str(sizes[0]) if len(sizes) == 1 else f"{sizes[0]} a {sizes[-1]}"


def section_summary(summary: dict[str, Any], a: str, b: str, judge: dict[str, Any] | None) -> list[str]:
    ov = summary["providers"]
    pa, pb = ov[a]["overall"], ov[b]["overall"]
    paired = summary["paired"]["overall"]
    label, conclusive = paired_status(paired)
    by_cat = summary["paired"]["by_task_type"]
    n_conclusive = sum(1 for blk in by_cat.values() if paired_status(blk)[1])
    d = paired["mean_diff_a_minus_b"]
    mc = paired["mcnemar"]
    who = short_name(a) if d >= 0 else short_name(b)
    lines = [
        f"1. Escopo: {short_name(a)} (qwen2.5:3b) contra {short_name(b)} (llama3.2:3b) via gateway, em "
        f"{pa['n']} prompts sintéticos em PT-BR ({per_category_n(summary)} por categoria). Não mede a perda em "
        "relação a um modelo de mercado; nenhuma chamada à Anthropic.",
        f"2. Neste conjunto: aprovação de {pct(pa['pass_rate'])} (IC95% {ci_text(pa['pass_rate_ci95'])}) para o "
        f"{short_name(a)} e {pct(pb['pass_rate'])} (IC95% {ci_text(pb['pass_rate_ci95'])}) para o {short_name(b)}.",
        f"3. Pareado, neste conjunto: diferença média de score {num(d)} (IC95% "
        f"{ci_text(paired['diff_ci95'], as_pct=False)}), McNemar {mc['only_a_passes']} x {mc['only_b_passes']}, "
        f"p={num(mc['p_exact'], 4)}; " + (f"diferença detectada a favor do {who}, " if conclusive
                                          else "indicativo, não conclusivo, ")
        + "sem generalizar para outros modelos ou domínios.",
        f"4. Por categoria ({per_category_n(summary)} pares cada), " + (
            "nenhuma comparação é conclusiva: todas são \"indicativo, não conclusivo\"." if n_conclusive == 0
            else f"{n_conclusive} de {len(by_cat)} comparações são conclusivas; as demais são \"indicativo, não "
                 "conclusivo\"."),
    ]
    if judge:
        meets = [j["decision"].get("meets_criterion") for j in judge["judges"].values()]
        lines.append("5. O juiz local (Qwen e Llama julgando) " + (
            "NÃO é confiável: nenhum dos dois atinge o critério fixado antes da execução; a summarization "
            "segue só pelo scorer determinístico e pela revisão humana." if not any(meets)
            else "atinge o critério em pelo menos um dos juízes; veja a Seção 6."))
    else:
        lines.append("5. O juiz local não foi executado neste run.")
    return ["## 1. Resumo", ""] + lines + [""]


def section_scope() -> list[str]:
    out = ["## 2. Escopo e avisos", ""]
    out += [f"{i}. {w}" for i, w in enumerate(WARNINGS, 1)]
    return out + [""]


def section_method(meta: dict[str, Any], summary: dict[str, Any], gpu_name: str | None) -> list[str]:
    scoring = meta.get("scoring", {})
    emb = scoring.get("embedding_info") or summary.get("embedding_info") or {}
    commit = meta.get("git_commit") or "n/d"
    dirty = meta.get("git_dirty")
    providers = meta.get("providers", {})
    models = ", ".join(f"{short_name(p)} = `{v.get('model')}`" for p, v in providers.items())
    ds = summary.get("dataset_hash_run") or meta.get("dataset_hash") or "n/d"
    n_per = {p: v["overall"]["n"] for p, v in summary["providers"].items()}
    return [
        "## 3. Método", "",
        f"- **Dataset:** sintético, PT-BR, {len(CATEGORY_ORDER)} categorias ({', '.join(CATEGORY_ORDER)}) x "
        f"{per_category_n(summary)} prompts = {next(iter(n_per.values()), 0)} prompts por provedor. Hash SHA-256 "
        f"`{ds[:12]}` (completo: `{ds}`); o hash no momento da pontuação é "
        + ("o mesmo." if summary.get("dataset_hash_now") == ds else "DIFERENTE (!)."),
        f"- **Modelos:** {models}. Ollama {meta.get('ollama_version', 'n/d')}.",
        "- **Execução:** um provedor por vez, em sequência, via gateway com provedor forçado, `temperature` 0, "
        "uma execução por prompt (sem repetição), warm-up descartado antes da medição, `context_window` 3072, "
        "`timeout` 120 s. A latência é medida no cliente, ponta a ponta (inclui o gateway). "
        f"Hash do config de avaliação `{str(meta.get('config_hash', 'n/d'))[:12]}`.",
        "- **Scorers (determinísticos):** por regra, `json_schema` (subconjunto de JSON Schema com valores "
        "esperados), `exact`, `regex`, `numeric`, `label_in` e `constraints`; erro de execução ou resposta vazia "
        "contam como `errors` e ficam fora da média, da aprovação e da latência. Summarization: 0,6 cobertura de "
        "pontos-chave + 0,3 similaridade por embedding + 0,1 tamanho; aprovado com score >= 0,7.",
        f"- **Embedding:** `{emb.get('model', 'n/d')}`, revision `{emb.get('revision', 'n/d')}`, "
        f"device `{emb.get('device', 'n/d')}`, `max_seq_length` {emb.get('max_seq_length', 'n/d')}.",
        "- **Estatística:** IC de Wilson 95% para a aprovação; bootstrap percentil (10 000 reamostragens, semente "
        f"{summary.get('seed', 'n/d')}) para a diferença média pareada; McNemar exato bilateral. Só se declara "
        "diferença quando o IC95% exclui zero E o McNemar dá p < 0,05 E há pelo menos 5 pares.",
        f"- **Commit do código na coleta:** `{commit}`" + (" (árvore com alterações não commitadas na coleta)"
                                                         if dirty else "") + ".",
        f"- **GPU:** {gpu_name or 'não informada (nvidia-smi indisponível)'}.",
        "",
        "### 3.1 Divulgação: correção do scorer de summarization", "",
        "Na primeira revisão de uma amostra de respostas, achou-se um defeito do scorer de summarization: em limites "
        "de \"N passos/tópicos\", a frase de abertura da resposta era contada como um item da lista. A correção "
        "(só as linhas de lista contam como passo) é de um BUG de contagem, coberta por teste; não foi um ajuste de "
        "pesos nem de limiar a partir do resultado, e o `STATUS.md` do projeto não registra nenhuma outra mudança "
        "no scorer. Os scores deste relatório são pós-correção: foram calculados em "
        f"{str((meta.get('scoring') or {}).get('scored_at', 'n/d'))[:19].replace('T', ' ')} UTC (`meta.json`, "
        "`scoring.scored_at`), com o scorer já corrigido. O que NÃO dá para confirmar: o histórico do git tem um "
        "único commit do scorer (feito depois da coleta e depois da correção), então ele não prova a ordem exata dos "
        "eventos nem o que mudou antes desse commit, e o relatório não afirma quantos scores a correção alterou.",
        "",
    ]


def results_table(summary: dict[str, Any], providers: Sequence[str]) -> list[str]:
    rows = []
    for p in providers:
        block = summary["providers"][p]
        for label, g in [("geral", block["overall"])] + [(c, block["by_task_type"][c]) for c in
                                                         order_categories(list(block["by_task_type"]))]:
            rows.append([short_name(p), label, g["n"], g["errors"], num(g["mean_score"]), pct(g["pass_rate"]),
                         ci_text(g["pass_rate_ci95"])])
    return table(["Provedor", "Categoria", "n", "Erros", "Score médio", "Aprovação", "IC95% (Wilson)"], rows)


def paired_table(summary: dict[str, Any]) -> list[str]:
    paired = summary["paired"]
    rows = []
    for label, blk in [("geral", paired["overall"])] + [(c, paired["by_task_type"][c]) for c in
                                                        order_categories(list(paired["by_task_type"]))]:
        mc = blk["mcnemar"]
        status, _ = paired_status(blk)
        rows.append([label, blk["pairs"], num(blk["mean_diff_a_minus_b"]), ci_text(blk["diff_ci95"], as_pct=False),
                     f"{mc['only_a_passes']} x {mc['only_b_passes']}", num(mc["p_exact"], 4), status])
    return table(["Categoria", "Pares", "Dif. média de score", "IC95% da diferença", "McNemar", "p exato",
                  "Leitura"], rows)


def section_results(summary: dict[str, Any], rows: list[dict[str, Any]], providers: Sequence[str]) -> list[str]:
    a, b = summary["paired"]["a"], summary["paired"]["b"]
    paired = summary["paired"]
    out = ["## 4. Resultados", "", "### 4.1 Por provedor e categoria", ""]
    out += results_table(summary, providers)
    out += ["Aprovação = fração de respostas com `passed` (sobre respostas avaliáveis). Com 8 prompts por "
            "categoria os ICs são largos. As aprovações da summarization são um teto (aviso 5). Em `reasoning` o "
            "enunciado proíbe explicar (aviso 3).", "",
            f"### 4.2 Comparação pareada ({short_name(a)} menos {short_name(b)})", ""]
    out += paired_table(summary)
    out += ["Dif. média = média por prompt de (score do " + short_name(a) + " menos score do " + short_name(b) + "). "
            f"McNemar = prompts em que só o {short_name(a)} aprova x prompts em que só o {short_name(b)} aprova. "
            "O resultado geral pode ser afirmado \"neste conjunto\"; as linhas por categoria só valem como "
            "\"indicativo, não conclusivo\" quando a leitura diz isso, e nesse caso não nomeiam vencedor. "
            "Nenhuma causa foi testada.", "",
            "### 4.3 Prompts em que os modelos reprovam (ids)", "",
            "- Os dois reprovam: " + (", ".join(f"`{i}`" for i in paired["both_fail"]) or "nenhum") + ".",
            ]
    only: dict[str, list[str]] = {}
    for item in paired["only_one_fails"]:
        only.setdefault(item["fails"], []).append(item["prompt_id"])
    for prov in providers:
        ids = only.get(prov, [])
        out.append(f"- Só o {short_name(prov)} reprova ({len(ids)}): " + (", ".join(f"`{i}`" for i in ids) or "nenhum")
                   + ".")
    out += ["", "Estas listas descrevem prompts, não um ranking por categoria.",]
    if any(i["prompt_id"] == "instruction-07" for i in paired["only_one_fails"]):
        out += ["", "A reprovação em `instruction-07` está correta, mas por motivo acidental do scorer (aviso 6): "
                + W_INSTRUCTION]
    out += ["", "### 4.4 Obediência de formato (`format_clean`)", ""]
    fm = []
    for p in providers:
        mine = [r for r in rows if r["provider"] == p and not r["errored"] and r["format_clean"] is not None]
        dirty = sorted(r["prompt_id"] for r in mine if r["format_clean"] is False)
        fm.append([short_name(p), len(mine), len(dirty), ", ".join(f"`{i}`" for i in dirty) or "nenhum"])
    out += table(["Provedor", "Respostas avaliáveis", "Precisaram de limpeza", "ids"], fm)
    out += ["`format_clean` falso = a resposta veio com cerca de código ou prosa ao redor e foi limpa antes de "
            "pontuar. A correção do conteúdo (`passed`) é medida depois da limpeza, então este número mostra a "
            "obediência de formato separada da correção.", ""]
    return out


def median(values: list[float]) -> float | None:
    s = sorted(values)
    if not s:
        return None
    mid = len(s) // 2
    return float(s[mid]) if len(s) % 2 else (s[mid - 1] + s[mid]) / 2


def short_categories(rows: list[dict[str, Any]]) -> set[str]:
    """Categorias de resposta curta: mediana de `completion_tokens` (sobre os dois provedores) abaixo do limite."""
    by_cat: dict[str, list[float]] = {}
    for r in rows:
        if not r["errored"] and r["completion_tokens"] is not None:
            by_cat.setdefault(r["task_type"], []).append(r["completion_tokens"])
    return {c for c, v in by_cat.items() if (median(v) or 0) < SHORT_ANSWER_TOKENS}


def section_latency(summary: dict[str, Any], rows: list[dict[str, Any]], providers: Sequence[str]) -> list[str]:
    short = short_categories(rows)
    out = ["## 5. Latência e tok/s", "", W_LATENCY, ""]
    table_rows = []
    for p in providers:
        block = summary["providers"][p]
        for label, g in [("geral", block["overall"])] + [(c, block["by_task_type"][c]) for c in
                                                         order_categories(list(block["by_task_type"]))]:
            comparable = "-" if label == "geral" else ("não (resposta curta)" if label in short else "sim")
            table_rows.append([short_name(p), label, num(g["latency_p50_ms"], 0), num(g["latency_p95_ms"], 0),
                               num(g["tokens_per_s_p50"], 1), comparable])
    out += table(["Provedor", "Categoria", "p50 (ms)", "p95 (ms)", "tok/s p50", "tok/s comparável"], table_rows)
    out += ["Latência ponta a ponta no cliente (inclui o gateway). tok/s = `completion_tokens` / tempo da "
            "requisição (inclui o processamento do prompt). Categorias de resposta curta (mediana abaixo de "
            f"{SHORT_ANSWER_TOKENS} tokens): " + (", ".join(f"`{c}`" for c in sorted(short)) or "nenhuma")
            + ". Uma execução por prompt, sem repetição; as diferenças de poucos milissegundos entre provedores "
            "não devem ser interpretadas.", ""]
    return out


def section_judge(judge: dict[str, Any] | None) -> list[str]:
    out = ["## 6. Juiz local", ""]
    if not judge:
        return out + ["O juiz local não foi executado neste run (`judge_summary.json` ausente).", ""]
    crit = judge["criterion"]
    names = list(judge["judges"])
    versions = sorted({str(j["prompt_version"]) for j in judge["judges"].values()})
    out += [
        "Um modelo de 3B julgando respostas de modelos de 3B. O juiz NÃO é tratado como verdade: mede-se primeiro "
        "a concordância dele com os scorers determinísticos e só então se decide quanto confiar.", "",
        "**Critério, fixado antes da execução** (em `evalkit/judge_stats.py`): "
        f"kappa >= {num(crit.get('kappa_min'), 1)} e acurácia >= {pct(crit.get('accuracy_min'), 0)}, por juiz, "
        f"no conjunto verificável (nota geral >= {crit.get('approve_min_overall')} = aprova; "
        f"{crit.get('set')}). Kappa indefinido reprova.", "",
        "### 6.1 Calibração contra o scorer (conjunto verificável)", ""]
    rows = []
    base = None
    for n in names:
        v = judge["judges"][n]["verifiable"]
        cm = v.get("confusion", {})
        base = v.get("scorer_pass_rate")
        rows.append([short_name(n) + "-juiz", v.get("n"), pct(v.get("accuracy")), ci_text(v.get("accuracy_ci95")),
                     num(v.get("kappa")), ci_text(v.get("kappa_ci95"), as_pct=False), pct(v.get("judge_approval_rate")),
                     f"{cm.get('tp')}/{cm.get('fp')}/{cm.get('fn')}/{cm.get('tn')}"])
    out += table(["Juiz", "n", "Acurácia", "IC95%", "Kappa", "IC95% do kappa", "Aprovação do juiz",
                  "VP/FP/FN/VN"], rows)
    if base is not None:
        rel = []
        for n in names:
            acc = judge["judges"][n]["verifiable"].get("accuracy")
            if acc is not None:
                rel.append(f"{short_name(n)}-juiz {pct(acc)} ({'abaixo' if acc < base else 'acima'} da linha, "
                           f"{num(abs(acc - base) * 100, 1)} p.p.)")
        out += [f"**Linha de base \"aprovar tudo\":** acurácia {pct(base)} (é a taxa de aprovação do scorer) e "
                "kappa 0. Comparação: " + "; ".join(rel) + ".", ""]
    out += ["**Veredito:** " + "; ".join(
        f"{short_name(n)}-juiz: {judge['judges'][n]['decision'].get('label', 'n/d')}" for n in names) + ".", ""]
    out += ["As notas dos dois juízes ficam marcadas \"NÃO CONFIÁVEL\" e não entram nos resultados da Seção 4. A "
            "summarization segue só pelo scorer determinístico e pela revisão humana.", "",
            "__H62__", ""]
    cats = order_categories(list(judge["judges"][names[0]]["by_task_type"]))
    sizes = sorted({judge["judges"][n]["by_task_type"].get(c, {}).get("n") for n in names for c in cats} - {None})
    size = f"n={sizes[0]} cada" if len(sizes) == 1 else "n pequeno"
    out[out.index("__H62__")] = f"### 6.2 Por categoria ({size}, IC largo)"
    rows = []
    for c in cats:
        row: list[Any] = [c]
        for n in names:
            v = judge["judges"][n]["by_task_type"].get(c, {})
            row += [pct(v.get("accuracy"), 0), num(v.get("kappa"))]
        rows.append(row)
    head = ["Categoria"] + [f"{short_name(n)}-juiz {m}" for n in names for m in ("acurácia", "kappa")]
    out += table(head, rows)
    inst = next(iter(judge["judges"].values()))["instruction_extra"]
    out += [f"`instruction` é a única categoria em que os dois juízes concordam bem com o scorer (acurácia "
            f"{pct(inst.get('accuracy'), 0)}, kappa {num(inst.get('kappa'))}, IC95% do kappa "
            f"{ci_text(inst.get('kappa_ci95'), as_pct=False)}, n={inst.get('n')}). É uma exceção de amostra "
            "pequena, não uma licença para usar o juiz nas outras categorias; nas demais, veja a tabela acima.", "",
            "### 6.3 Comparação pareada pelo juiz", ""]
    rows = []
    for n in names:
        p = judge["judges"][n]["pairwise"]
        fp = p.get("final_preference", {})
        rows.append([short_name(n) + "-juiz", pct(p.get("position_bias_index"), 0),
                     pct(p.get("order_inconsistency"), 0), fp.get("ollama_qwen"), fp.get("ollama_llama"),
                     fp.get("tie")])
    out += table(["Juiz", "Viés de posição", "Inconsistência de ordem", "Prefere Qwen", "Prefere Llama",
                  "Empates"], rows)
    out += ["- **Viés de posição** = prompts em que as duas rodadas (com a ordem trocada) escolheram modelos "
            "diferentes, ou seja, o juiz repetiu a mesma POSIÇÃO, dividido pelos prompts válidos: "
            + "; ".join(f"{short_name(n)}-juiz {judge['judges'][n]['pairwise'].get('flipped')}/"
                        f"{judge['judges'][n]['pairwise'].get('prompts')}" for n in names) + ".",
            "- **Inconsistência de ordem** = (prompts com troca de lado + prompts em que uma rodada deu empate e a "
            "outra escolheu um lado) dividido pelos prompts válidos: "
            + "; ".join(f"{short_name(n)}-juiz ({judge['judges'][n]['pairwise'].get('flipped')} + "
                        f"{judge['judges'][n]['pairwise'].get('tie_vs_pick')})/"
                        f"{judge['judges'][n]['pairwise'].get('prompts')}" for n in names) + ". Não é a mesma "
            "medida do viés: só coincide quando nenhuma rodada deu empate contra uma escolha.", ""]
    for n in names:
        p = judge["judges"][n]["pairwise"]
        ties = (p.get("final_preference") or {}).get("tie")
        inconsistent = (p.get("flipped") or 0) + (p.get("tie_vs_pick") or 0)
        if ties is not None:
            out.append(f"- {short_name(n)}-juiz: {ties} empates finais de {p.get('prompts')} prompts = {inconsistent} "
                       f"por inconsistência de ordem + {ties - inconsistent} em que as duas rodadas empataram; só "
                       f"{p.get('prompts', 0) - ties} prompts tiveram preferência decisiva.")
    out += ["", "Com tanta inconsistência e quase todos os resultados em empate, o pareado do juiz é inutilizável "
            "como medida de qualidade.", "", "### 6.4 Entre juízes, autopreferência e injeção", ""]
    cj = judge.get("cross_judge", {})
    pw_, pr_ = cj.get("pairwise", {}), cj.get("pointwise", {})
    sp = judge.get("self_preference", {})
    own = {n: sp.get(n, {}) for n in names}
    out += [
        f"- **Entre juízes:** concordância de aprovação {pct(pr_.get('approval_agreement'), 0)} (kappa "
        f"{num(pr_.get('approval_kappa'))}, Spearman das notas {num(pr_.get('spearman_overall'))}); no pareado, "
        f"{pct(pw_.get('preference_agreement'), 0)} (kappa {num(pw_.get('preference_kappa'))}). As notas não são "
        "promediadas.",
        "- **Autopreferência: sem evidência suficiente.** Com dois modelos e dois juízes, o contraste observado é "
        "UM só (no pareado o número é idêntico para os dois juízes por construção) e não permite dizer qual juiz "
        "favorece o próprio modelo; parte do efeito pode ser real (o Qwen acerta mais no conjunto "
        "verificável, explicação possível e não testada). Aprovações indevidas (scorer reprovou, juiz aprovou): "
        + "; ".join(f"{short_name(n)}-juiz {own[n].get('false_approvals_on_own_answers', {}).get('judge_approved')}/"
                    f"{own[n].get('false_approvals_on_own_answers', {}).get('scorer_failed')} nas próprias e "
                    f"{own[n].get('false_approvals_on_other_answers', {}).get('judge_approved')}/"
                    f"{own[n].get('false_approvals_on_other_answers', {}).get('scorer_failed')} nas do outro"
                    for n in names) + ". Não há evidência suficiente nem para afirmar nem para descartar viés.",
        "- **Injeção de prompt: sem evidência suficiente.** A defesa (marcadores neutralizados, prompt de sistema "
        "que não depende da resposta) é coberta por teste; a observação ao vivo teve 1 resposta por juiz e não "
        "separa manipulação de leniência. Não se afirma resistência.",
        "- **Summarization:** correlação de Spearman das notas do juiz com o scorer: "
        + ", ".join(f"{short_name(n)}-juiz {num(judge['judges'][n]['summarization'].get('spearman'))}"
                    + (" (nota constante, indefinida)" if judge['judges'][n]['summarization'].get('spearman') is None
                       else "") for n in names)
        + f" (n={judge['judges'][names[0]]['summarization'].get('n')} respostas; intervalo muito largo).", "",
        "### 6.5 Divulgação: o prompt do juiz mudou uma vez", "",
        "O prompt do juiz teve uma única alteração, da versão v1 para a v2, por um bug de formato: o exemplo de "
        "saída trazia a faixa (`1-5`) no lugar de uma nota e o juiz a copiava. A correção mexeu só no exemplo de "
        "formato; o critério de decisão não foi tocado e o prompt NÃO foi ajustado a partir de concordância. O "
        "problema foi visto no JSON bruto de um teste-fumaça de 12 prompts (numa cópia do run), antes da execução "
        "completa; nesse teste e na execução completa nenhum juiz chegou perto do critério. Versão usada nos "
        "números acima: " + ", ".join(f"`{v}`" for v in versions) + ".", ""]
    return out


def section_human(human: dict[str, Any] | None) -> list[str]:
    out = ["## 7. Revisão humana", ""]
    if not human:
        return out + ["A revisão humana não foi registrada para este run (`human_verdicts.json` ausente).", ""]
    items = human["items"]
    core = [i for i in items if i["group"] != "extra_tool_use"]
    extra = [i for i in items if i["group"] == "extra_tool_use"]
    verif = [i for i in core if i["task_type"] != "summarization"]
    summ = [i for i in core if i["task_type"] == "summarization"]
    n_agree = sum(1 for i in verif if i["group"] == "agree")
    n_dis = sum(1 for i in verif if i["group"] == "disagree")

    def agree(group: list[dict[str, Any]]) -> str:
        return f"{sum(1 for i in group if i['human_agrees_scorer'])}/{len(group)}"

    out += [
        f"O revisor humano olhou {len(core)} itens por sorteio estratificado (semente {human.get('seed')}): "
        f"{len(verif)} de tarefas verificáveis (dos quais {n_agree} foram escolhidos entre os casos em que os dois "
        f"juízes concordam com o scorer e {n_dis} entre os casos em que algum juiz discorda) e {len(summ)} de "
        f"summarization, e mais {len(extra)} itens de `tool_use` "
        "escolhidos depois (respostas do Qwen que o scorer aprovou e o Qwen-juiz reprovou). Para cada item viu o "
        "enunciado, a resposta, o resultado do scorer e as notas dos juízes. Registro: " + human["statement"] + ".",
        "", "**Como ler esta seção:** há um único revisor, a amostra é de " + str(len(core)) + " itens e o revisor "
        "VIU a pontuação do scorer ao decidir (possível ancoragem). Por isso a concordância abaixo é uma verificação "
        "de sanidade, e não uma validação independente do scorer.", "",
        "### 7.1 Concordância humano x scorer (verificação de sanidade)", ""]
    rows = [["Todos os itens do sorteio", len(core), agree(core)],
            ["Verificáveis", len(verif), agree(verif)],
            ["Summarization", len(summ), agree(summ)]]
    out += table(["Conjunto", "n", "Humano concorda com o scorer"], rows)
    out += [f"Os {len(extra)} itens extras de `tool_use` ({agree(extra)} de concordância) foram escolhidos por uma "
            "característica (o Qwen-juiz reprovou) e ficam FORA da tabela: seriam uma amostra enviesada.", "",
            f"**Ressalva de amostra pequena:** n={len(core)} no total ({len(verif)} verificáveis, {len(summ)} de "
            f"summarization). Uma concordância de {agree(core)} com n assim não valida o scorer: o intervalo de "
            "confiança é largo e o revisor decidiu vendo o resultado do scorer. Os itens escolhidos por discordância "
            "entre juiz e scorer estão sobrerrepresentados.", "",
            "### 7.2 Juiz x humano", "",
            "A comparação juiz x humano não é reportada porque o humano decidiu vendo o resultado do scorer (logo "
            "coincide com ele) e a amostra foi escolhida para incluir discordâncias; a calibração do juiz está na "
            "Seção 6.1.", "",
            "### 7.3 Ressalvas do revisor (sem mudança no scorer nem no dataset)", ""]
    by_n = {i["n"]: i["prompt_id"] for i in items}
    for c in human.get("caveats", []):
        ref = ", ".join(f"{n} (`{by_n.get(n, '?')}`)" for n in c["items"])
        out.append(f"- Item(ns) {ref}: {c['text']}.")
    return out + [""]


def section_limits(summary: dict[str, Any], meta: dict[str, Any]) -> list[str]:
    total = next(iter(summary["providers"].values()))["overall"]["n"]
    dirty = [
        "- A coleta do run foi feita com alterações ainda não commitadas: o commit citado na Seção 3 não reproduz "
        "exatamente o código usado, e por isso a reprodução exata a partir do commit não é possível. Os hashes do "
        "dataset e do config permitem verificar essas duas partes."] if meta.get("git_dirty") else []
    return [
        "## 8. Limitações", "",
        f"- Compara só dois modelos de 3B, em um conjunto sintético de {total} prompts em PT-BR "
        f"({per_category_n(summary)} por categoria); nada "
        "aqui se generaliza para outros modelos, tamanhos ou domínios.",
        "- Não há baseline de um modelo de mercado: não se sabe quanto qualidade se perde em relação a ele.",
        "- Por categoria, os ICs são largos e nenhuma comparação é conclusiva; só o resultado geral pareado é "
        "afirmado, e apenas \"neste conjunto\".",
        "- Uma execução por prompt, `temperature` 0, sem repetição: não mede a variação entre execuções.",
        "- A referência de qualidade são regras escritas à mão (e, na summarization, pontos-chave); regras podem "
        "errar para os dois lados (avisos 5 e 6). `reasoning` pode penalizar respostas sem explicação "
        "(aviso 3; efeito não medido).",
        "- Latências de modelo quente em GPU, uma máquina, sem concorrência (aviso 4).",
        "- O juiz local não é confiável; a revisão humana cobre 16 itens (mais 3 extras), foi feita por uma só "
        "pessoa que viu a pontuação do scorer, e é uma verificação de sanidade, não uma validação independente.",
        "- Sem causalidade testada: diferenças observadas não são atribuídas a nenhuma causa.", *dirty, ""]


def section_next() -> list[str]:
    return [
        "## 9. Próximos passos", "",
        "1. **Fase 3b, baseline do Claude:** rodar os mesmos prompts em um modelo de mercado para medir a perda "
        "real. Depende de autorização explícita (envolve chamadas pagas) e de uma fase própria.",
        "2. **Variante de `reasoning` com explicação liberada**, para separar a capacidade de raciocinar do "
        "efeito da proibição de explicar (aviso 3).",
        "3. **Mais prompts por categoria**, para que as comparações por categoria possam ser conclusivas.",
        "4. **Fase 2, LiteLLM:** comparar o gateway com o LiteLLM Proxy no mesmo cenário de fallback, rate limit "
        "e cooldown.", ""]


# --------------------------------------------------------------------------- montagem
def build_markdown(*, run_id: str, summary: dict[str, Any], meta: dict[str, Any], rows: list[dict[str, Any]],
                   judge: dict[str, Any] | None, human: dict[str, Any] | None, gpu_name: str | None) -> str:
    a, b = summary["paired"]["a"], summary["paired"]["b"]
    providers = [a, b]
    date = str((meta.get("scoring") or {}).get("scored_at") or meta.get("created_at") or "")[:10] or "n/d"
    out = [f"# Fase 3: Qwen contra Llama via gateway | run `{run_id}`", "",
           f"Dados do run de {date} (data lida do `meta.json`). Este relatório traz só agregados e ids de prompts; "
           "nenhum texto de resposta de modelo.", ""]
    out += section_summary(summary, a, b, judge)
    out += section_scope()
    out += section_method(meta, summary, gpu_name)
    out += section_results(summary, rows, providers)
    out += section_latency(summary, rows, providers)
    out += section_judge(judge)
    out += section_human(human)
    out += section_limits(summary, meta)
    out += section_next()
    return "\n".join(out).rstrip("\n") + "\n"


def check_consistency(summary: dict[str, Any], rows: list[dict[str, Any]]) -> None:
    counts = Counter(r["provider"] for r in rows)
    for p, block in summary["providers"].items():
        if counts.get(p, 0) != block["overall"]["n"]:
            raise ReportError(f"summary.json e scores.jsonl divergem em {p}: "
                              f"{block['overall']['n']} contra {counts.get(p, 0)} (rode evalkit.score de novo)")


def raw_texts(results: dict[str, list[dict[str, Any]]], judge_raw: dict[str, Any] | None) -> list[str]:
    """Textos que NÃO podem aparecer no relatório: respostas dos modelos e motivos escritos pelos juízes."""
    texts = [as_text(rec.get("response")) for recs in results.values() for rec in recs]

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            for k, v in node.items():
                if k == "reason" and isinstance(v, str):
                    texts.append(v)
                else:
                    walk(v)
        elif isinstance(node, list):
            for v in node:
                walk(v)

    walk(judge_raw)
    return [t.strip() for t in texts if len(t.strip()) >= MIN_LEAK_CHARS]


def find_leaks(outputs: Sequence[str], forbidden: Sequence[str]) -> int:
    """Quantos textos proibidos aparecem (como trecho) em alguma das saídas."""
    return sum(1 for t in set(forbidden) if any(t in o for o in outputs))


def detect_gpu() -> str | None:
    exe = shutil.which("nvidia-smi")
    if not exe:
        return None
    try:
        res = subprocess.run([exe, "--query-gpu=name", "--format=csv,noheader"], capture_output=True, text=True,
                             timeout=15, check=False)
    except (OSError, subprocess.SubprocessError):
        return None
    names = [ln.strip() for ln in res.stdout.splitlines() if ln.strip()]
    return names[0] if res.returncode == 0 and names else None


def execute(run_id: str, results_root: Path = RESULTS_ROOT, out_dir: Path = REPORTS_DIR,
            gpu_name: str | None = None, out: Callable[[str], None] = print) -> tuple[Path, Path]:
    run_dir = results_root / run_id
    if not run_dir.is_dir():
        raise ReportError(f"run não encontrado: {run_dir}")
    summary = load_json(run_dir / "summary.json", required=True)
    meta = load_json(run_dir / "meta.json", required=True)
    judge_raw = load_json(run_dir / "judge_summary.json", required=False)
    human = load_json(run_dir / "human_verdicts.json", required=False)
    assert summary is not None and meta is not None
    rows = load_score_rows(run_dir)
    check_consistency(summary, rows)
    md = build_markdown(run_id=run_id, summary=summary, meta=meta, rows=rows,
                        judge=judge_view(judge_raw) if judge_raw else None, human=human, gpu_name=gpu_name)
    csv_text = build_csv(summary, [summary["paired"]["a"], summary["paired"]["b"]])
    leaks = find_leaks([md, csv_text], raw_texts(load_run(run_dir), judge_raw))
    if leaks:
        raise ReportError(f"{leaks} trecho(s) de resposta/motivo apareceriam no relatório; nada foi gravado")
    out_dir.mkdir(parents=True, exist_ok=True)
    md_path, csv_path = out_dir / f"{REPORT_BASENAME}.md", out_dir / f"{REPORT_BASENAME}.csv"
    for path, text in ((md_path, md), (csv_path, csv_text)):
        with path.open("w", encoding="utf-8", newline="\n") as fh:
            fh.write(text)
    out(f"relatório gravado: {md_path.name} e {csv_path.name} em {out_dir}")
    return md_path, csv_path


def parse_args(argv: list[str] | None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Relatório final da Fase 3 (sem rede, sem Anthropic).")
    p.add_argument("--run-id", required=True)
    p.add_argument("--out-dir", type=Path, default=REPORTS_DIR)
    p.add_argument("--gpu", default=None, help="modelo da GPU (padrão: lido do nvidia-smi, se houver)")
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        execute(args.run_id, out_dir=args.out_dir, gpu_name=args.gpu or detect_gpu())
    except ScoreError as e:
        print(f"erro: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
