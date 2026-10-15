"""Geração das tabelas e do CSV do comparativo a partir de resultados FICTÍCIOS (Marco 14).

Sem litellm, sem Ollama, sem rede. As execuções fictícias carregam respostas-canário nos campos brutos
(`reply`, `error_body`, `headers`): nenhuma pode aparecer no CSV nem no documento.
"""

import csv
import io
import random

import pytest

from comparekit import report
from comparekit.features import FEATURES, METHODS, Feature

CANARY_REPLY = "CANARY-REPLY-7f3a9c-the-model-said-this"
CANARY_ERROR = "CANARY-ERROR-BODY-91bd22-upstream-text"
META = {"run_id": "fake", "litellm_version": "1.103.1"}
NETGUARD = {"proxy_starts": 2, "armed_python_processes": 2, "monitor_samples": 5, "blocked": [],
            "external_connections": [], "orphans": [], "allowed_events": {"socket.connect -> 127.0.0.1:mock": 3}}


def make_run(scene, gateway, rep, steps, received, time_to_mock=None, extra=None):
    """`steps` = [(status, provider, attempts, reached_mock)]."""
    return {
        "scene": scene, "gateway": gateway, "rep": rep, "title": f"titulo {scene}",
        "requests": [{"i": i, "label": "x", "t_s": 0.5 * i, "status": s, "provider": p, "attempts": a,
                      "reached_mock": r, "reply": CANARY_REPLY, "error_body": CANARY_ERROR,
                      "headers": {"x-canary": CANARY_REPLY}, "latency_ms": 10 + i}
                     for i, (s, p, a, r) in enumerate(steps, start=1)],
        "mock": {"received": received, "by_status": {"200": received}}, "time_to_mock_s": time_to_mock,
        "extra": {**(extra or {}), "reply": CANARY_REPLY},
    }


def fake_runs():
    runs = []
    for rep in (1, 2, 3):
        runs += [
            make_run("1", "proprio", rep, [(200, "mock_market", 0, True)] * 3, 3),
            make_run("1", "litellm", rep, [(200, "mock_market", 0, True)] * 3, 3),
            make_run("3a", "proprio", rep, [(200, "ollama_qwen", 1, True), (200, "mock_market", 0, True)], 2,
                     time_to_mock=2.0 + 0.1 * rep),
            make_run("3a", "litellm", rep, [(200, "ollama_qwen", 1, True), (200, "mock_market", 0, True)], 2,
                     time_to_mock=0.0),
            make_run("4", "proprio", rep, [(200, "mock_market", 0, True)] * 3 + [(200, "ollama_qwen", 1, False)] * 3, 3,
                     extra={"calls_until_stop": 3}),
            # o LiteLLM diverge na repetição 3 (quatro chamadas em vez de três)
            make_run("4", "litellm", rep, [(200, "mock_market", 0, True)] * (4 if rep == 3 else 3), 4 if rep == 3 else 3,
                     extra={"calls_until_stop": 4 if rep == 3 else 3}),
        ]
    return runs


# --------------------------------------------------------------------------- determinismo
def test_csv_is_deterministic_and_independent_of_input_order():
    runs = fake_runs()
    first = report.render_csv(runs)
    for seed in (1, 2, 3):
        shuffled = runs[:]
        random.Random(seed).shuffle(shuffled)
        assert report.render_csv(shuffled) == first
    assert report.render_csv(runs) == first  # duas execuções, mesma saída


def test_blocks_are_deterministic_and_independent_of_input_order():
    runs = fake_runs()
    first = report.render_generated_block(runs, META, NETGUARD)
    shuffled = runs[:]
    random.Random(7).shuffle(shuffled)
    assert report.render_generated_block(shuffled, META, NETGUARD) == first
    assert report.render_generated_block(runs, META, NETGUARD) == first


def test_csv_columns_and_row_order():
    rows = list(csv.DictReader(io.StringIO(report.render_csv(fake_runs()))))
    assert tuple(rows[0]) == report.CSV_FIELDS
    keys = [(r["cena"], r["gateway"], int(r["repeticao"])) for r in rows]
    assert keys == sorted(keys, key=lambda k: (report.SCENE_ORDER.index(k[0]), report.GATEWAY_ORDER.index(k[1]), k[2]))
    first = rows[0]
    assert (first["cena"], first["gateway"], first["repeticao"]) == ("1", "proprio", "1")
    assert first["provedor"] == "mock_market x3" and first["chamadas_ao_mock"] == "3" and first["status"] == "200 x3"
    timed = next(r for r in rows if (r["cena"], r["gateway"], r["repeticao"]) == ("3a", "proprio", "1"))
    assert timed["tempo_ate_voltar_s"] == "2.10" and timed["tentativas"] == "1"
    assert next(r for r in rows if r["cena"] == "1")["tempo_ate_voltar_s"] == ""


# --------------------------------------------------------------------------- tabelas
def test_behavior_table_marks_divergence_between_repetitions():
    table = report.behavior_table(fake_runs(), META)
    line4_own = next(line for line in table.splitlines() if line.startswith("| 4:") and "gateway próprio" in line)
    line4_lite = next(line for line in table.splitlines() if line.startswith("| 4:") and "LiteLLM 1.103.1" in line)
    assert "3/3 iguais" in line4_own and "DIVERGE" not in line4_own
    assert "DIVERGE: 3 (2/3); 4 (1/3)" in line4_lite  # a divergência é reportada, não escolhida
    assert "DIVERGE (2 resultados distintos em 3)" in line4_lite


def test_time_cell_reports_range_and_missing_measurements():
    runs = [r for r in fake_runs() if r["scene"] == "3a" and r["gateway"] == "proprio"]
    assert "2.1 a 2.3" in report.behavior_table(runs, META)
    runs[1]["time_to_mock_s"] = None
    assert "DIVERGE: 1/3 não voltaram" in report.behavior_table(runs, META)
    for r in runs:
        r["time_to_mock_s"] = None
    assert "não voltou no limite da sondagem" in report.behavior_table(runs, META)


def test_derived_table_lists_scene_metrics_per_gateway():
    table = report.derived_table(fake_runs(), META)
    row = next(line for line in table.splitlines() if line.startswith("| 4 |"))
    assert "| 3 | DIVERGE: 3 (2/3); 4 (1/3) |" in row


# --------------------------------------------------------------------------- canários
def test_no_model_text_reaches_csv_or_document():
    runs = fake_runs()
    for text in (report.render_csv(runs), report.render_generated_block(runs, META, NETGUARD)):
        assert CANARY_REPLY not in text and CANARY_ERROR not in text and "CANARY" not in text


def test_sanitize_drops_everything_outside_the_whitelist():
    clean = report.sanitize_run(fake_runs()[0])
    flat = repr(clean)
    assert "CANARY" not in flat and "latency_ms" not in flat and "headers" not in flat
    assert set(clean["requests"][0]) == {"i", "t_s", "status", "provider", "attempts", "reached_mock"}


def test_provider_field_cannot_smuggle_text():
    for smuggled in (f"o modelo disse {CANARY_REPLY}", CANARY_REPLY * 2):  # com espaço; longo demais
        run = make_run("1", "proprio", 1, [(200, smuggled, 0, True)], 1)
        assert report.sanitize_run(run)["requests"][0]["provider"] == "?"


def test_find_leaks_blocks_output_that_repeats_raw_text():
    runs = fake_runs()
    assert report.find_leaks(f"tabela com {CANARY_ERROR} no meio", runs) == [CANARY_ERROR]
    assert report.find_leaks("tabela limpa", runs) == []
    with pytest.raises(report.ReportError):
        report.update_doc("sem marcadores", "x")


def test_generated_block_refuses_to_write_when_a_leak_is_found(monkeypatch):
    runs = fake_runs()
    monkeypatch.setattr(report, "behavior_table", lambda *_: f"| {CANARY_REPLY} |")
    with pytest.raises(report.ReportError, match="repetiria texto"):
        report.render_generated_block(runs, META, NETGUARD)


# --------------------------------------------------------------------------- avisos obrigatórios
def test_every_mandatory_warning_is_in_the_generated_block():
    block = report.render_generated_block(fake_runs(), META, NETGUARD)
    assert len(report.WARNINGS) == 4
    for warning in report.WARNINGS:
        assert warning in block


def test_block_explains_how_to_read_attempts_and_stability():
    block = report.render_generated_block(fake_runs(), META, NETGUARD)
    for note in report.HOW_TO_READ:
        assert note in block
    assert "não são chamadas feitas" in block


def test_warnings_state_the_required_scope():
    text = " ".join(report.WARNINGS)
    for phrase in ("1.103.1", "setembro/2026", "Windows", "só-config", "mock", "não devem ser generalizados",
                   "versões diferentes", "COMPORTAMENTO", "não de qualidade geral nem de desempenho",
                   "projeto de estudo", "muito mais completo", "Latências", "indicativas"):
        assert phrase in text, phrase


# --------------------------------------------------------------------------- documento e recursos
def test_update_doc_replaces_only_the_generated_region():
    doc = f"antes\n{report.BEGIN}\nvelho\n{report.END}\ndepois\n"
    new = report.update_doc(doc, f"{report.BEGIN}\nnovo\n{report.END}")
    assert new == f"antes\n{report.BEGIN}\nnovo\n{report.END}\ndepois\n"
    assert report.update_doc(new, f"{report.BEGIN}\nnovo\n{report.END}") == new  # idempotente


def test_features_only_accept_known_verification_methods():
    with pytest.raises(ValueError, match="forma de verificacao invalida"):
        Feature("x", "a", "b", "achismo")
    assert {f.method for f in FEATURES} <= set(METHODS)
    table = report.features_table([Feature("Métricas", "sim", "não", "experimento", "nota")])
    assert "| Métricas | sim | não | experimento (nota) |" in table


def test_features_cover_the_required_topics():
    names = " ".join(f.name.lower() for f in FEATURES)
    for topic in ("fallback", "prometheus", "gravação", "orçamento", "streaming", "chaves virtuais"):
        assert topic in names, topic
    out_of_scope = [f for f in FEATURES if "streaming" in f.name.lower() or "chaves virtuais" in f.name.lower()]
    assert out_of_scope and all("fora do escopo" in (f.own + f.litellm + f.note).lower() for f in out_of_scope)


# --------------------------------------------------------------------------- junção de execuções e trava
def test_merge_runs_combines_runs_and_sums_guard_totals():
    a = ([make_run("1", "proprio", 1, [(200, "mock_market", 0, True)], 1)],
         {"run_id": "a", "litellm_version": None}, {**NETGUARD, "proxy_starts": 2})
    b = ([make_run("3c", "litellm", 1, [(200, "mock_market", 0, True)], 1)],
         {"run_id": "b", "litellm_version": "1.103.1"}, {**NETGUARD, "proxy_starts": 3})
    probes = {**NETGUARD, "proxy_starts": 1, "orphans": [7]}
    runs, meta, guard = report.merge_runs([a, b], [probes])
    assert [r["scene"] for r in runs] == ["1", "3c"]
    assert meta["sources"] == ["a", "b"] and meta["litellm_version"] == "1.103.1"
    assert meta["scenes"] == ["1", "3c"]
    assert guard["proxy_starts"] == 6 and guard["orphans"] == [7]
    assert guard["allowed_events"] == {"socket.connect -> 127.0.0.1:mock": 9}


def test_merge_runs_refuses_the_same_run_twice():
    part = ([make_run("1", "proprio", 1, [(200, "mock_market", 0, True)], 1)], {"run_id": "a"}, NETGUARD)
    with pytest.raises(report.ReportError, match="repetida"):
        report.merge_runs([part, part])


def test_dynamic_loopback_ports_collapse_into_one_line():
    allowed = {"socket.connect -> '127.0.0.1':51007": 1, "socket.connect -> '127.0.0.1':51009": 1,
               "asyncio.sock_connect -> '127.0.0.1':mock": 4}
    out = report.collapse_allowed(allowed)
    assert out["asyncio.sock_connect -> '127.0.0.1':mock"] == 4
    assert out["socket.connect -> 127.0.0.1 (porta dinâmica, loopback do próprio proxy)"] == 2
    assert len(out) == 2
