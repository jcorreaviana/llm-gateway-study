"""Tabelas, CSV e bloco gerado do `docs/LITELLM_COMPARISON.md` a partir dos resultados do runner.

    python -m comparekit.report --run-id m14-full

Determinístico (mesma entrada, mesma saída, em qualquer ordem das execuções). Só agregados e ids: as
execuções passam por `sanitize_run` (lista de campos permitidos) e `find_leaks` recusa gravar se algum
texto de resposta ou de erro dos resultados brutos aparecer na saída. Nenhum texto de resposta de modelo
é lido por este módulo.
"""

from __future__ import annotations

import argparse
import csv
import io
import json
import re
import sys
from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path
from typing import Any

from comparekit import metrics
from comparekit.features import FEATURES, METHODS, Feature

ROOT = Path(__file__).resolve().parent.parent
RESULTS_ROOT = ROOT / "compare_results"
DOC_PATH = ROOT / "docs" / "LITELLM_COMPARISON.md"
CSV_PATH = ROOT / "reports" / "fase2-gateway-vs-litellm.csv"
BEGIN = "<!-- BEGIN GENERATED: comparekit.report (nao editar a mao; rode python -m comparekit.report) -->"
END = "<!-- END GENERATED: comparekit.report -->"

SCENE_ORDER = ("1", "2", "3a", "3b", "3c", "3d", "4", "5a", "5b", "6", "7", "7b", "8a", "8b", "9")
SCENE_TITLES = {
    "1": "Normal", "2": "Limite local (rpm 10, 13 requisições seguidas)",
    "3a": "429 com Retry-After de 2 s", "3b": "429 com Retry-After de 15 s",
    "3c": "429 repetidos com Retry-After de 2 s (até 8 requisições)",
    "3d": "429 repetidos com Retry-After de 15 s (até 8 requisições)",
    "4": "Falha 5xx contínua (10 requisições)", "5a": "Falhas intercaladas: 2 falhas, 1 sucesso, repetido 4 vezes",
    "5b": "Falhas intercaladas: falha, sucesso, repetido 5 vezes",
    "6": "Recuperação (mock volta lento, sondagem a cada 0,25 s)",
    "7": "Tudo indisponível (Ollama apontado para porta sem serviço, mock em 429)",
    "7b": "Tudo indisponível (Ollama apontado para porta sem serviço, mock em 500)",
    "8a": "Prompt maior que todas as janelas (~44 mil caracteres)",
    "8b": "Prompt maior que a janela do Ollama, menor que a do mock (~16 mil caracteres, mock em 429)",
    "9": "Forçar provedor",
}
TIMED_SCENES = frozenset({"3a", "3b", "3c", "3d", "6"})
GATEWAY_ORDER = ("proprio", "litellm_af2", "litellm")

# Avisos obrigatórios (testados: o bloco gerado SEMPRE os contém).
W_VERSION = ("Os resultados valem para o LiteLLM 1.103.1 (setembro/2026), em um Windows, no modo só-config, em um "
             "cenário específico com o mock, e não devem ser generalizados; versões diferentes podem se comportar "
             "de modo diferente.")
W_SCOPE = "A comparação é de COMPORTAMENTO num cenário, não de qualidade geral nem de desempenho."
W_PROJECT = ("O gateway próprio é um projeto de estudo e o LiteLLM é um produto muito mais completo e muito mais "
             "amplo, que não foi avaliado aqui além deste cenário.")
W_LATENCY = "Latências de uma máquina, sem concorrência e com poucas repetições são só indicativas."
WARNINGS = (W_VERSION, W_SCOPE, W_PROJECT, W_LATENCY)

HOW_TO_READ = (
    "**Tentativas de fallback** não são chamadas feitas: no gateway próprio é o número de provedores listados em "
    "`x-gateway-fallback-reason` (inclui os pulados sem chamada, como `cooling_down` e `context_too_large`); no "
    "LiteLLM é o valor de `x-litellm-attempted-fallbacks`. A medida de quantas chamadas CHEGARAM ao provedor é a "
    "coluna do mock (`/stats` do mock, zerado a cada repetição).",
    "**Tempo até voltar ao mock**: do fim da requisição que provocou o cooldown (ou da queda do mock) até o envio da "
    "primeira requisição que chegou ao mock; a resolução é o intervalo de sondagem (0,5 s nas cenas 3a e 3b, 0,25 s na "
    "cena 6). Dispersão de até 1 s entre repetições é tratada como o mesmo resultado.",
    "**Coluna do config versionado do LiteLLM**: nas cenas de falha (3c, 3d, 4, 5a, 5b e 6) o contador de falhas está "
    "inativo (`allowed_fails: 3` é o padrão da biblioteca), então \"não parou\" ali é efeito dessa configuração, não "
    "uma limitação do produto; a coluna comparável ao gateway próprio é a de `allowed_fails: 2`. Nas cenas 1, 2, 3a, "
    "3b, 7, 7b, 8a, 8b e 9 só o config versionado foi executado (o contador não está em jogo, ou um 429 só não o "
    "ativa).",
    "**Estabilidade**: as 3 repetições têm os mesmos status, provedores, tentativas e chamadas ao mock. Quando "
    "divergem, a tabela mostra cada valor com a contagem, sem escolher um.",
)

CSV_FIELDS = ("cena", "gateway", "repeticao", "requisicoes", "status", "provedor", "tentativas",
              "chamadas_ao_mock", "tempo_ate_voltar_s")
EXTRA_KEYS = {"calls_until_stop": "chamadas ao mock até o gateway parar de chamá-lo",
              "first_skipped": "1ª requisição que deixou de chegar ao mock",
              "tripped": "o gateway parou de chamar o mock antes da recuperação",
              "probe_window": "na janela da 1ª chamada lenta: (respondidas pelo mock, respondidas por outro)",
              "mock_calls_after_recovery": "chamadas ao mock depois que ele voltou"}
_ID = re.compile(r"^[A-Za-z0-9_.:-]{1,40}$")
LEAK_KEYS = frozenset({"reply", "body", "error_body", "text", "content", "message", "response"})


class ReportError(Exception):
    pass


# --------------------------------------------------------------------------- saneamento
def _ident(value: Any) -> str:
    return value if isinstance(value, str) and _ID.match(value) else "?"


def _num(value: Any) -> int | float | None:
    return value if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def _extra_value(value: Any) -> Any:
    if isinstance(value, bool) or value is None:
        return value
    if isinstance(value, (int, float)):
        return value
    if isinstance(value, (list, tuple)) and all(isinstance(v, (int, float)) for v in value):
        return list(value)
    return None


def sanitize_run(run: Mapping[str, Any]) -> dict[str, Any]:
    """Só campos numéricos, booleanos e ids curtos; qualquer outro campo (texto de resposta, de erro, headers)
    é descartado aqui, antes de chegar a uma tabela."""
    reqs = []
    for r in run.get("requests", []):
        reqs.append({"i": int(r["i"]), "t_s": float(r["t_s"]), "status": int(r["status"]),
                     "provider": _ident(r.get("provider")), "attempts": _num(r.get("attempts")),
                     "reached_mock": bool(r.get("reached_mock"))})
    mock = run.get("mock") or {}
    return {
        "scene": _ident(run["scene"]), "gateway": _ident(run["gateway"]), "rep": int(run["rep"]),
        "requests": sorted(reqs, key=lambda r: (r["t_s"], r["i"])),
        "mock": {"received": int(mock.get("received", 0))},
        "time_to_mock_s": _num(run.get("time_to_mock_s")),
        "extra": {k: _extra_value(v) for k, v in (run.get("extra") or {}).items() if k in EXTRA_KEYS},
    }


def _strings_under_leak_keys(node: Any, under: bool = False) -> Iterable[str]:
    if isinstance(node, Mapping):
        for k, v in node.items():
            yield from _strings_under_leak_keys(v, under or k in LEAK_KEYS)
    elif isinstance(node, (list, tuple)):
        for v in node:
            yield from _strings_under_leak_keys(v, under)
    elif isinstance(node, str) and under:
        yield node


def find_leaks(output: str, raw_runs: Sequence[Mapping[str, Any]], min_len: int = 8) -> list[str]:
    """Textos de resposta/erro dos resultados brutos que aparecem inteiros na saída."""
    return sorted({s for s in _strings_under_leak_keys(list(raw_runs)) if len(s) >= min_len and s in output})


# --------------------------------------------------------------------------- linhas e agregação
def _scene_key(scene: str) -> tuple[int, str]:
    return (SCENE_ORDER.index(scene) if scene in SCENE_ORDER else len(SCENE_ORDER), scene)


def _gateway_key(gateway: str) -> tuple[int, str]:
    return (GATEWAY_ORDER.index(gateway) if gateway in GATEWAY_ORDER else len(GATEWAY_ORDER), gateway)


def run_row(run: Mapping[str, Any]) -> dict[str, Any]:
    reqs = run["requests"]
    t = run["time_to_mock_s"]
    return {"cena": run["scene"], "gateway": run["gateway"], "repeticao": run["rep"], "requisicoes": len(reqs),
            "status": metrics.rle(r["status"] for r in reqs), "provedor": metrics.rle(r["provider"] for r in reqs),
            "tentativas": metrics.attempts_total(reqs), "chamadas_ao_mock": run["mock"]["received"],
            "tempo_ate_voltar_s": "" if t is None else f"{t:.2f}"}


def render_csv(runs: Iterable[Mapping[str, Any]]) -> str:
    rows = sorted((run_row(sanitize_run(r)) for r in runs),
                  key=lambda r: (_scene_key(r["cena"]), _gateway_key(r["gateway"]), r["repeticao"]))
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=CSV_FIELDS, lineterminator="\n")
    writer.writeheader()
    writer.writerows(rows)
    return buf.getvalue()


def _agg(values: Sequence[Any], fmt=str) -> str:
    """Um valor se as repetições concordam; senão lista cada valor distinto com a contagem."""
    counts = Counter(fmt(v) for v in values)
    if len(counts) == 1:
        return next(iter(counts))
    return "DIVERGE: " + "; ".join(f"{v} ({n}/{len(values)})" for v, n in sorted(counts.items()))


def _time_cell(scene: str, runs: Sequence[Mapping[str, Any]]) -> str:
    values = [r["time_to_mock_s"] for r in runs]
    summary = metrics.time_summary(values)
    if summary["min"] is None:
        return "não voltou no limite da sondagem" if scene in TIMED_SCENES else "—"
    lo, hi = f"{summary['min']:.1f}", f"{summary['max']:.1f}"
    base = lo if lo == hi else f"{lo} a {hi}"
    if None in values:
        return f"{base} (DIVERGE: {values.count(None)}/{len(values)} não voltaram)"
    return base if metrics.time_stable(values) else f"{base} (varia mais de {metrics.TIME_TOLERANCE_S:.0f} s)"


def group_runs(runs: Iterable[Mapping[str, Any]]) -> dict[tuple[str, str], list[dict[str, Any]]]:
    grouped: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for run in runs:
        clean = sanitize_run(run)
        grouped.setdefault((clean["scene"], clean["gateway"]), []).append(clean)
    for runs_ in grouped.values():
        runs_.sort(key=lambda r: r["rep"])
    return grouped


def stability(scene: str, runs: Sequence[Mapping[str, Any]]) -> str:
    n = len(runs)
    distinct = len({metrics.signature(r) for r in runs})
    same_time = metrics.time_stable([r["time_to_mock_s"] for r in runs]) if scene in TIMED_SCENES else True
    if distinct == 1 and same_time:
        return f"{n}/{n} iguais"
    return f"DIVERGE ({distinct} resultados distintos em {n})" if distinct > 1 else "DIVERGE (só no tempo)"


def _label(gateway: str, meta: Mapping[str, Any]) -> str:
    version = meta.get("litellm_version") or "?"
    if gateway == "litellm_af2":
        return f"LiteLLM {version} (allowed_fails: 2, comparável)"
    if gateway == "litellm":
        return (f"LiteLLM {version}, config versionado "
                "(contador de falhas inativo: allowed_fails: 3 = padrão da biblioteca)")
    return "gateway próprio" if gateway == "proprio" else gateway


def behavior_table(runs: Iterable[Mapping[str, Any]], meta: Mapping[str, Any]) -> str:
    grouped = group_runs(runs)
    lines = ["| Cena | Gateway | Requisições (status) | Provedor que respondeu | Tentativas de fallback | "
             "Chamadas que chegaram ao mock | Tempo até voltar ao mock (s) | Estabilidade |",
             "|---|---|---|---|---|---|---|---|"]
    for scene, gateway in sorted(grouped, key=lambda k: (_scene_key(k[0]), _gateway_key(k[1]))):
        rs = grouped[(scene, gateway)]
        lines.append("| {} | {} | {} | {} | {} | {} | {} | {} |".format(
            f"{scene}: {SCENE_TITLES.get(scene, scene)}", _label(gateway, meta),
            _agg([metrics.rle(q["status"] for q in r["requests"]) for r in rs]),
            _agg([metrics.rle(q["provider"] for q in r["requests"]) for r in rs]),
            _agg([metrics.attempts_total(r["requests"]) for r in rs]),
            _agg([r["mock"]["received"] for r in rs]), _time_cell(scene, rs), stability(scene, rs)))
    return "\n".join(lines)


def _extra_text(value: Any) -> str:
    return "não parou dentro da cena" if value is None else str(value)


def derived_table(runs: Iterable[Mapping[str, Any]], meta: Mapping[str, Any]) -> str:
    grouped = group_runs(runs)
    gateways = [g for g in GATEWAY_ORDER if any(key[1] == g for key in grouped)]
    rows = []
    for scene in SCENE_ORDER:
        for key, text in EXTRA_KEYS.items():
            cells = []
            present = False
            for gateway in gateways:
                rs = grouped.get((scene, gateway), [])
                values = [r["extra"][key] for r in rs if key in r["extra"]]
                present |= bool(values)
                cells.append(_agg(values, _extra_text) if values else "—")
            if present:
                rows.append(f"| {scene} | {text} | " + " | ".join(cells) + " |")
    header = "| Cena | Métrica | " + " | ".join(_label(g, meta) for g in gateways) + " |"
    divider = "|---|---|" + "---|" * len(gateways)
    return "\n".join([header, divider, *rows])


def features_table(features: Sequence[Feature] = FEATURES) -> str:
    lines = ["| Funcionalidade | Gateway próprio | LiteLLM | Como foi verificado |", "|---|---|---|---|"]
    for f in features:
        if f.method not in METHODS:
            raise ReportError(f"forma de verificacao invalida: {f.method!r}")
        how = f.method + (f" ({f.note})" if f.note else "")
        lines.append(f"| {f.name} | {f.own} | {f.litellm} | {how} |")
    return "\n".join(lines)


_DYNAMIC_LOOPBACK = re.compile(r"^(?P<event>[\w.]+) -> '127\.0\.0\.1':\d+$")


def collapse_allowed(allowed: Mapping[str, int]) -> dict[str, int]:
    """Conexões a portas dinâmicas de loopback (o próprio proxy conectando em si) viram uma linha só."""
    out: dict[str, int] = {}
    for name, count in allowed.items():
        m = _DYNAMIC_LOOPBACK.match(name)
        key = f"{m['event']} -> 127.0.0.1 (porta dinâmica, loopback do próprio proxy)" if m else name
        out[key] = out.get(key, 0) + count
    return out


def guard_summary(netguard: Mapping[str, Any]) -> str:
    allowed = collapse_allowed(netguard.get("allowed_events", {}))
    lines = [f"- Subidas do proxy: {netguard.get('proxy_starts', 0)}; processos Python com a trava armada: "
             f"{netguard.get('armed_python_processes', 0)}; amostras do monitor externo: "
             f"{netguard.get('monitor_samples', 0)}.",
             f"- Eventos BLOQUEADOS: {len(netguard.get('blocked', []))}; conexões fora do loopback vistas pelo "
             f"monitor externo: {len(netguard.get('external_connections', []))}; processos restantes: "
             f"{len(netguard.get('orphans', []))}.",
             "- Eventos permitidos (todos para loopback):"]
    lines += [f"  - {count} x `{name}`" for name, count in sorted(allowed.items())]
    return "\n".join(lines)


# --------------------------------------------------------------------------- bloco gerado e documento
def render_generated_block(runs: Sequence[Mapping[str, Any]], meta: Mapping[str, Any],
                           netguard: Mapping[str, Any]) -> str:
    reps = sorted({r["rep"] for r in runs})
    parts = [BEGIN, "", "> **Avisos obrigatórios**", ">", *[f"> - {w}" for w in WARNINGS], "",
             f"Execução `{meta.get('run_id', '?')}`: {len(reps)} repetições por cena e por gateway, o gateway e o mock "
             "reiniciados a cada repetição; cada requisição usa `temperature` 0 e `max_tokens` 16.", "",
             "**Como ler as tabelas**", "", *[f"- {h}" for h in HOW_TO_READ], "",
             "### Comportamento por cena", "", behavior_table(runs, meta), "",
             "### Métricas derivadas", "", derived_table(runs, meta), "",
             "### Recursos e observabilidade", "", features_table(), "",
             "### Trava de rede do LiteLLM (todas as subidas do proxy)", "", guard_summary(netguard), "", END]
    block = "\n".join(parts)
    leaks = find_leaks(block, runs)
    if leaks:
        raise ReportError(f"o relatorio repetiria texto dos resultados brutos ({len(leaks)} trecho(s)); nada gravado")
    return block


def update_doc(doc: str, block: str) -> str:
    if BEGIN in doc and END in doc:
        head, rest = doc.split(BEGIN, 1)
        _, tail = rest.split(END, 1)
        return head + block + tail
    raise ReportError("marcadores BEGIN/END GENERATED nao encontrados no documento")


def load_run(run_id: str) -> tuple[list[dict[str, Any]], dict[str, Any], dict[str, Any]]:
    base = RESULTS_ROOT / run_id
    data = json.loads((base / "results.json").read_text(encoding="utf-8"))
    netguard = json.loads((base / "netguard.json").read_text(encoding="utf-8"))
    return data["runs"], data["meta"], netguard


def merge_runs(parts: Sequence[tuple[list[dict[str, Any]], dict[str, Any], dict[str, Any]]],
               extra_guards: Sequence[Mapping[str, Any]] = ()
               ) -> tuple[list[dict[str, Any]], dict[str, Any], dict[str, Any]]:
    """Junta execuções do runner (ex.: a principal e uma complementar com cenas novas). Recusa repetição da
    mesma (cena, gateway, repetição); soma os totais da trava de rede; registra as origens no meta."""
    runs: list[dict[str, Any]] = []
    seen: set[tuple[str, str, int]] = set()
    for part_runs, _, _ in parts:
        for run in part_runs:
            key = (run["scene"], run["gateway"], run["rep"])
            if key in seen:
                raise ReportError(f"execucao repetida ao juntar resultados: {key}")
            seen.add(key)
            runs.append(run)
    meta = dict(parts[0][1])
    meta["sources"] = [m.get("run_id") for _, m, _ in parts]
    meta["scenes"] = sorted({r["scene"] for r in runs}, key=_scene_key)
    meta["litellm_version"] = next((m.get("litellm_version") for _, m, _ in parts if m.get("litellm_version")), None)
    guard: dict[str, Any] = {"proxy_starts": 0, "armed_python_processes": 0, "monitor_samples": 0, "blocked": [],
                             "external_connections": [], "orphans": [], "allowed_events": {}}
    for g in [*(g for _, _, g in parts), *extra_guards]:
        for k in ("proxy_starts", "armed_python_processes", "monitor_samples"):
            guard[k] += g.get(k, 0)
        for k in ("blocked", "external_connections", "orphans"):
            guard[k] += g.get(k, [])
        for name, count in g.get("allowed_events", {}).items():
            guard["allowed_events"][name] = guard["allowed_events"].get(name, 0) + count
    return runs, meta, guard


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--also", action="append", default=[], help="outra execucao a juntar (repetivel)")
    parser.add_argument("--guard-from", action="append", default=[],
                        help="execucao so de diagnostico: soma apenas a trava de rede (repetivel)")
    parser.add_argument("--doc", type=Path, default=DOC_PATH)
    parser.add_argument("--csv", type=Path, default=CSV_PATH)
    args = parser.parse_args(argv)
    try:
        probes_guard = RESULTS_ROOT / args.run_id / "netguard-probes.json"  # sondas de recursos (mesma trava)
        extra = [json.loads(probes_guard.read_text(encoding="utf-8"))] if probes_guard.exists() else []
        extra += [load_run(r)[2] for r in args.guard_from]
        runs, meta, netguard = merge_runs([load_run(r) for r in (args.run_id, *args.also)], extra)
        block = render_generated_block(runs, meta, netguard)
        csv_text = render_csv(runs)
        if find_leaks(csv_text, runs):
            raise ReportError("o CSV repetiria texto dos resultados brutos; nada gravado")
        doc = update_doc(args.doc.read_text(encoding="utf-8"), block)
    except ReportError as exc:
        print(f"erro: {exc}", file=sys.stderr)
        return 2
    args.csv.parent.mkdir(parents=True, exist_ok=True)
    args.csv.write_text(csv_text, encoding="utf-8", newline="")
    args.doc.write_text(doc, encoding="utf-8", newline="")
    print(f"gravado {args.csv} e o bloco gerado de {args.doc}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
