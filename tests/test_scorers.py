"""Scorers determinísticos (Fase 3): casos dourados, incluindo saídas difíceis. Sem rede e sem Ollama."""

import hashlib
import json
import re

import pytest

from evalkit.embedding import EmbeddingUnavailable, cosine
from evalkit.schema import EvalPrompt
from evalkit.scorers import (
    ExactOptions,
    as_text,
    count_sentences,
    count_words,
    extract_json,
    key_point_coverage,
    labels_found,
    last_number,
    normalize_exact,
    parse_number,
    score_constraints,
    score_exact,
    score_json_schema,
    score_label_in,
    score_numeric,
    score_record,
    score_regex,
    size_limit,
)

# --------------------------------------------------------------------------- json_schema
PERSON = {
    "schema": {
        "type": "object",
        "properties": {"nome": {"type": "string"}, "idade": {"type": "integer"}, "ativo": {"type": "boolean"}},
        "required": ["nome", "idade", "ativo"],
    },
    "expected": {"nome": "Ana Souza", "idade": 30, "ativo": True},
}
GOOD = {"nome": "Ana Souza", "idade": 30, "ativo": True}
ONE_LINE = json.dumps(GOOD)
INDENTED = json.dumps(GOOD, indent=2)


@pytest.mark.parametrize(
    ("text", "clean"),
    [
        (ONE_LINE, True),
        (INDENTED, True),
        (f"\n  {ONE_LINE}  \n", True),
        (f"```json\n{INDENTED}\n```", False),
        (f"```\n{ONE_LINE}\n```", False),
        (f"Claro! Aqui está o resultado:\n{INDENTED}\nEspero ter ajudado.", False),
        (f"Resultado: {ONE_LINE}. Fim.", False),
    ],
)
def test_json_schema_perfect_in_every_wrapping_but_format_clean_differs(text, clean):
    r = score_json_schema(text, PERSON)
    assert (r.score, r.passed) == (1.0, True)
    assert r.details["format_clean"] is clean


def test_json_schema_partial_credit_is_fraction_of_correct_fields():
    r = score_json_schema('{"nome": "Ana Souza", "idade": 31, "ativo": true}', PERSON)
    assert r.passed is False
    assert r.score == pytest.approx(2 / 3)
    assert list(r.details["wrong_fields"]) == ["idade"]


def test_json_schema_missing_wrong_type_and_extra_prose():
    r = score_json_schema('{"nome": "Ana Souza", "idade": "30"}', PERSON)
    assert r.passed is False
    assert r.score == pytest.approx(1 / 3)
    assert set(r.details["wrong_fields"]) == {"idade", "ativo"}


def test_json_schema_text_compare_ignores_case_and_spaces_numbers_by_value():
    spec = {"schema": PERSON["schema"], "expected": {"nome": "ana souza", "idade": 30}}
    assert score_json_schema('{"nome": " ANA SOUZA ", "idade": 30.0, "ativo": false}', spec).passed is True


def test_json_schema_bool_is_not_a_number():
    spec = {"schema": PERSON["schema"], "expected": {"idade": 1}}
    assert score_json_schema('{"nome": "x", "idade": true, "ativo": true}', spec).passed is False


NESTED = {
    "schema": {
        "type": "object",
        "properties": {
            "name": {"type": "string", "enum": ["somar", "subtrair"]},
            "arguments": {
                "type": "object",
                "properties": {"a": {"type": "number"}, "b": {"type": "number"}, "texto": {"type": "string"}},
                "required": ["a", "b", "texto"],
            },
        },
        "required": ["name", "arguments"],
    },
    "expected": {"name": "somar", "arguments": {"a": 17, "b": 25}},
}


def test_json_schema_nested_required_field_without_expected_must_still_exist():
    ok = score_json_schema('{"name": "somar", "arguments": {"a": 17, "b": 25, "texto": "qualquer"}}', NESTED)
    assert (ok.score, ok.passed) == (1.0, True)
    missing = score_json_schema('{"name": "somar", "arguments": {"a": 17, "b": 25}}', NESTED)
    assert missing.passed is False and set(missing.details["wrong_fields"]) == {"arguments.texto"}


def test_json_schema_enum_violation():
    r = score_json_schema('{"name": "dividir", "arguments": {"a": 17, "b": 25, "texto": "x"}}', NESTED)
    assert r.passed is False and "name" in r.details["wrong_fields"]


def test_json_schema_array_instead_of_object_and_garbage_score_zero():
    assert score_json_schema(f"[{ONE_LINE}]", PERSON).score == 0.0
    garbage = score_json_schema("não consegui gerar o JSON", PERSON)
    assert (garbage.score, garbage.passed, garbage.details["json_found"]) == (0.0, False, False)


def test_json_schema_additional_properties_false_counts_as_error():
    spec = {"schema": {**PERSON["schema"], "additionalProperties": False}, "expected": PERSON["expected"]}
    r = score_json_schema('{"nome": "Ana Souza", "idade": 30, "ativo": true, "extra": 1}', spec)
    assert r.passed is False and r.score < 1.0


def test_json_schema_array_value_compared_whole():
    spec = {
        "schema": {
            "type": "object",
            "properties": {"numeros": {"type": "array", "items": {"type": "number"}}},
            "required": ["numeros"],
        },
        "expected": {"numeros": [7, 8.5, 9]},
    }
    assert score_json_schema('{"numeros": [7, 8.5, 9]}', spec).passed is True
    assert score_json_schema('{"numeros": [7, 9, 8.5]}', spec).passed is False


def test_extract_json_skips_invalid_braces_in_prose():
    value, clean, err = extract_json('Use {chaves} assim: {"a": 1} e pronto')
    assert (value, clean, err) == ({"a": 1}, False, None)


# --------------------------------------------------------------------------- exact
@pytest.mark.parametrize(
    ("text", "ok"),
    [("Clara", True), ("clara", True), ("  Clara.  ", True), ('"Clara"', True), ("Clara\n", True),
     ("A Clara", False), ("Clara é a mais baixa", False), ("Bia", False)],
)
def test_exact_default_normalization(text, ok):
    assert score_exact(text, {"value": "Clara"}).passed is ok


def test_exact_normalization_is_configurable():
    assert normalize_exact("Águá!", ExactOptions(accents=True, punctuation="all")) == "agua"
    assert normalize_exact("Águá!", ExactOptions(casefold=False)) == "Águá"
    assert score_exact("Cafe", {"value": "Café"}).passed is False
    assert score_exact("Cafe", {"value": "Café"}, ExactOptions(accents=True)).passed is True
    assert normalize_exact("a   b\tc") == "a b c"


def test_exact_format_clean_flag():
    assert score_exact("Clara", {"value": "Clara"}).details["format_clean"] is True
    assert score_exact("clara.", {"value": "Clara"}).details["format_clean"] is False


# --------------------------------------------------------------------------- regex
def test_regex_search_with_flags():
    spec = {"pattern": r"^ol[aá]\b", "flags": "im"}
    assert score_regex("Bom dia\nOLÁ, mundo", spec).passed is True
    assert score_regex("nada", spec).passed is False
    fenced = score_regex("```\nola\n```", {"pattern": r"^ola$"})  # a cerca é tirada antes de casar
    assert fenced.passed is True and fenced.details["format_clean"] is False
    assert score_regex("x\nola", {"pattern": r"^ola$"}).passed is False  # sem flag m


# --------------------------------------------------------------------------- numeric
@pytest.mark.parametrize(
    ("token", "value"),
    [("15", 15), ("1.234,5", 1234.5), ("1,5", 1.5), ("1.234", 1234), ("3.5", 3.5), ("1,234.5", 1234.5),
     ("1.234.567", 1234567), ("0.500", 0.5), ("-7", -7), ("1,234,567", 1234567), ("3.499,90", 3499.9)],
)
def test_parse_number_formats(token, value):
    assert parse_number(token) == pytest.approx(value)


@pytest.mark.parametrize(
    ("text", "want", "ok"),
    [
        ("15", 15, True),
        ("15 reais", 15, True),
        ("R$ 15,00", 15, True),
        ("O resultado é 12 - 5 + 8 = 15.", 15, True),
        ("Maria tem 15 maçãs.", 15, True),
        ("Primeiro 20 depois 15", 15, True),
        ("15 depois 20", 15, False),
        ("São 1.234,5 reais", 1234.5, True),
        ("1.234,5", 1234, False),
        ("sem número", 3, False),
        ("", 3, False),
        ("-7 graus", -7, True),
    ],
)
def test_numeric_takes_last_number(text, want, ok):
    assert score_numeric(text, {"value": want}).passed is ok


def test_numeric_tolerance_and_format_clean():
    assert score_numeric("3,14", {"value": 3.1416, "tolerance": 0.01}).passed is True
    assert score_numeric("3,2", {"value": 3.1416, "tolerance": 0.01}).passed is False
    assert score_numeric("15", {"value": 15}).details["format_clean"] is True
    assert score_numeric("15 reais", {"value": 15}).details["format_clean"] is False
    assert last_number("nada")[0] is None


# --------------------------------------------------------------------------- label_in
LABELS = {"labels": ["positivo", "negativo", "neutro"], "expected": "positivo"}


@pytest.mark.parametrize(
    ("text", "ok"),
    [("positivo", True), ("Positivo.", True), ("  POSITIVO\n", True), ("A mensagem é positivo", True),
     ("negativo", False), ("positivo ou negativo", False), ("positivo, negativo", False), ("", False),
     ("não sei", False), ("positivos", False)],
)
def test_label_in_whole_word_and_single_label(text, ok):
    assert score_label_in(text, LABELS).passed is ok


def test_label_in_two_labels_is_a_failure_even_with_the_right_one():
    r = score_label_in("Pode ser positivo ou neutro.", LABELS)
    assert (r.score, r.passed) == (0.0, False)
    assert r.details["labels_found"] == ["positivo", "neutro"]
    assert "mais de um" in r.details["error"]


def test_label_in_repeating_the_same_label_is_fine():
    assert score_label_in("positivo, sem dúvida positivo", LABELS).passed is True


def test_label_in_underscore_accents_and_substrings():
    spec = {"labels": ["suporte_tecnico", "financeiro", "legitimo", "spam"], "expected": "suporte_tecnico"}
    assert score_label_in("suporte técnico", spec).passed is True
    assert labels_found("É legítimo", spec["labels"]) == ["legitimo"]
    assert labels_found("ilegitimo", spec["labels"]) == []  # palavra inteira


def test_label_in_format_clean():
    assert score_label_in("positivo", LABELS).details["format_clean"] is True
    assert score_label_in("positivo.", LABELS).details["format_clean"] is False


# --------------------------------------------------------------------------- constraints
def rules(*pairs):
    return {"rules": [{"kind": k, "value": v} for k, v in pairs]}


def test_constraints_score_is_fraction_of_rules_met():
    r = score_constraints("um dois três quatro", rules(("max_words", 3), ("min_words", 2), ("lowercase", True)))
    assert r.score == pytest.approx(2 / 3) and r.passed is False
    assert [x["passed"] for x in r.details["rules"]] == [False, True, True]


def test_constraints_lists_lines_and_sentences():
    text = "- a\n- b\n- c"
    assert score_constraints(text, rules(("n_items", 3), ("n_lines", 3))).passed is True
    assert score_constraints("1. a\n2) b", rules(("n_items", 2))).passed is True
    assert score_constraints("a\n\nb", rules(("n_lines", 2))).passed is True
    assert count_sentences("Oi. Tudo bem? Sim!") == 3
    assert score_constraints("Oi. Tudo bem?", rules(("n_sentences", 2))).passed is True


def test_constraints_words_are_whole_words_case_insensitive():
    forb = rules(("forbidden_words", ["água", "agua"]))
    assert score_constraints("Beba ÁGUA", forb).passed is False
    assert score_constraints("Beba aguardente", forb).passed is True
    req = rules(("required_words", ["sol", "lua"]))
    assert score_constraints("O Sol e a lua", req).passed is True
    assert score_constraints("O solar e a lua", req).passed is False


def test_constraints_chars_start_end_newline_lowercase():
    assert score_constraints("Café da manhã", rules(("starts_with", "Café"), ("max_chars", 13))).passed is True
    assert score_constraints("café", rules(("starts_with", "Café"))).passed is False
    assert score_constraints("Oi!\n", rules(("ends_with", "!"), ("no_newline", True))).passed is True
    assert score_constraints("a\nb", rules(("no_newline", True))).passed is False
    assert score_constraints("Oi", rules(("lowercase", True))).passed is False
    assert score_constraints("abc", rules(("min_chars", 4))).passed is False
    assert count_words("caixa-d'água azul") == 2


# --------------------------------------------------------------------------- resumo
class FakeEmbedder:
    """Determinístico: saco de palavras com hash (sem modelo, sem rede)."""

    def __init__(self, dim: int = 64):
        self.dim = dim
        self.calls = 0

    def embed(self, texts):
        self.calls += 1
        out = []
        for text in texts:
            v = [0.0] * self.dim
            for w in re.findall(r"\w+", text.casefold()):
                v[int(hashlib.md5(w.encode()).hexdigest(), 16) % self.dim] += 1.0
            out.append(v)
        return out


class BrokenEmbedder:
    def embed(self, texts):
        raise EmbeddingUnavailable("modelo sumiu")


def summary_prompt(instruction="Resuma o texto em até 2 frases."):
    return EvalPrompt.model_validate({
        "id": "summarization-01", "task_type": "summarization", "lang": "pt-BR",
        "messages": [{"role": "user", "content": f"{instruction}\n\nTexto:\nA ciclovia de 12 km liga o centro ao bairro."}],
        "params": {"temperature": 0, "max_tokens": 200},
        "reference_answer": "A nova ciclovia de 12 quilômetros liga o centro ao bairro da Serra.",
        "key_points": ["ciclovia de 12 km ligando o centro ao bairro da Serra", "custo de 3 milhões de reais"],
    })


def rec(text, **kw):
    return {"prompt_id": "x", "status": 200, "response": text, "error": None, **kw}


GOOD_SUMMARY = "A ciclovia de 12 km liga o centro ao bairro da Serra e custou 3 milhões de reais."


def test_key_point_coverage_ignores_accents_case_and_inflection():
    cov, per = key_point_coverage("CICLOVIAS de 12 KM ligam o CENTRO ao bairro da serra; custo: 3 milhoes", [
        "ciclovia de 12 km ligando o centro ao bairro da Serra", "custo de 3 milhões de reais"])
    assert cov == 1.0 and all(p["covered"] for p in per)


def test_summarization_with_embedder_is_composite_and_documented():
    r = score_record(summary_prompt(), rec(GOOD_SUMMARY), FakeEmbedder())
    assert r["errored"] is False and r["passed"] is True
    d = r["details"]
    assert d["embedding"] == "ok" and 0 < d["similarity"] <= 1
    assert set(d["weights"]) == {"coverage", "similarity", "length"}
    assert sum(d["weights"].values()) == pytest.approx(1.0)
    assert d["coverage"] == 1.0 and d["length"]["limit"] == {"unit": "frases", "n": 2, "exact": False}


@pytest.mark.parametrize("embedder", [None, BrokenEmbedder()])
def test_summarization_degrades_to_coverage_without_embedder(embedder):
    r = score_record(summary_prompt(), rec(GOOD_SUMMARY), embedder)
    assert r["passed"] is True and r["score"] == pytest.approx(1.0)
    assert r["details"]["embedding"].startswith("unavailable")
    assert "similarity" not in r["details"] and set(r["details"]["weights"]) == {"coverage", "length"}


def test_summarization_missing_points_and_length_lower_the_score():
    bad = score_record(summary_prompt(), rec("Algo sem relação nenhuma. Outra frase. Mais uma."), None)
    assert bad["passed"] is False and bad["score"] < 0.3
    assert bad["details"]["length"]["observed"] == 3


def test_summarization_truncated_by_max_tokens_fails_the_length_part():
    r = score_record(summary_prompt(), rec(GOOD_SUMMARY, finish_reason="length"), None)
    assert r["details"]["length"]["truncated"] is True and r["details"]["length"]["reason"]
    assert r["score"] < 1.0


def test_size_limit_parsing():
    assert size_limit("Resuma em até 3 frases") == {"unit": "frases", "n": 3, "exact": False}
    assert size_limit("Resuma o comunicado em uma frase.") == {"unit": "frases", "n": 1, "exact": True}
    assert size_limit("Resuma o texto em duas frases.") == {"unit": "frases", "n": 2, "exact": True}
    assert size_limit("Resuma em 3 tópicos curtos") == {"unit": "itens", "n": 3, "exact": True}
    assert size_limit("Resuma em 4 passos numerados") == {"unit": "itens", "n": 4, "exact": True}
    assert size_limit("Resuma o texto") is None


def test_cosine_basics():
    assert cosine([1, 0], [1, 0]) == pytest.approx(1.0)
    assert cosine([1, 0], [0, 1]) == 0.0
    assert cosine([0, 0], [1, 1]) == 0.0
    with pytest.raises(ValueError):
        cosine([1], [1, 2])


# --------------------------------------------------------------------------- erros do runner e robustez
def objective_prompt(check, task_type="reasoning"):
    return EvalPrompt.model_validate({
        "id": f"{task_type}-01", "task_type": task_type, "lang": "pt-BR",
        "messages": [{"role": "user", "content": "pergunta"}], "params": {"temperature": 0, "max_tokens": 20},
        "check": check,
    })


NUM = {"type": "numeric", "spec": {"value": 15}}


@pytest.mark.parametrize(
    ("record", "kind"),
    [
        ({"status": 0, "response": None, "error": "ReadTimeout: x"}, "timeout_or_connection"),
        ({"status": 503, "response": None, "error": "indisponível"}, "runner_error"),
        ({"status": 500, "response": None}, "http_error"),
        ({"status": 200, "response": ""}, "empty_response"),
        ({"status": 200, "response": "  \n\t "}, "empty_response"),
        ({"status": 200, "response": None}, "empty_response"),
    ],
)
def test_runner_errors_and_empty_answers_are_scored_zero_and_flagged_apart(record, kind):
    r = score_record(objective_prompt(NUM), {"prompt_id": "x", **record})
    assert (r["score"], r["passed"], r["errored"], r["error_kind"]) == (0.0, False, True, kind)


@pytest.mark.parametrize(
    "response",
    [None, 15, 15.5, ["15"], {"a": 1}, "15", "\x00\x01", "💥" * 50, "a" * 100_000],
    ids=["none", "int", "float", "list", "dict", "str", "control", "emoji", "huge"],
)
def test_never_raises_for_strange_input(response):
    for check_type, spec in [
        ("json_schema", PERSON), ("exact", {"value": "Clara"}), ("regex", {"pattern": "a"}), ("numeric", {"value": 1}),
        ("label_in", LABELS), ("constraints", rules(("max_words", 3))),
    ]:
        prompt = objective_prompt({"type": check_type, "spec": spec})
        out = score_record(prompt, {"prompt_id": "x", "status": 200, "response": response})
        assert set(out) >= {"score", "passed", "errored", "details"}
        assert 0.0 <= out["score"] <= 1.0
    sm = score_record(summary_prompt(), {"prompt_id": "x", "status": 200, "response": response}, FakeEmbedder())
    assert 0.0 <= sm["score"] <= 1.0


def test_a_broken_spec_becomes_scorer_error_not_an_exception():
    prompt = objective_prompt(NUM)
    object.__setattr__(prompt.check, "spec", {})  # contrato violado de propósito
    out = score_record(prompt, {"prompt_id": "x", "status": 200, "response": "15"})
    assert out["score"] == 0.0 and out["passed"] is False and "scorer_error" in out["details"]


def test_truncated_flag_recorded_for_objective_checks():
    out = score_record(objective_prompt(NUM), {"prompt_id": "x", "status": 200, "response": "15", "finish_reason": "length"})
    assert out["details"]["truncated"] is True and out["passed"] is True


def test_as_text():
    assert as_text(None) == "" and as_text("x") == "x" and as_text({"a": 1}) == '{"a": 1}'


def test_summarization_steps_ignore_an_intro_line_but_count_list_items():
    prompt = summary_prompt("Resuma as instruções em 4 passos numerados (1., 2., 3., 4.), um por linha.")
    intro = "Aqui estão os passos:\n\n1. ciclovia de 12 km ligando o centro ao bairro da Serra\n2. custo de 3 milhões de reais\n3. c\n4. d"
    ok = score_record(prompt, rec(intro), None)
    assert ok["details"]["length"]["observed"] == 4 and ok["score"] == pytest.approx(1.0)
    five = score_record(prompt, rec(intro + "\n5. e"), None)
    assert five["details"]["length"]["observed"] == 5 and five["score"] < 1.0
