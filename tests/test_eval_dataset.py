"""Schema e validador do dataset de avaliação (Fase 3). Sem rede e sem Ollama."""

import copy
import json
import subprocess
from collections import Counter
from pathlib import Path

import pytest
from pydantic import ValidationError

from evalkit import validate_dataset as vd
from evalkit.schema import TASK_TYPES, EvalPrompt
from evalkit.tokens import estimate_tokens
from gateway.providers import estimate_tokens as gateway_estimate_tokens

REPO_ROOT = Path(__file__).resolve().parent.parent
CONTEXT = 3072


def good(task_type="reasoning", n=1, **over):
    """Um prompt válido mínimo da categoria (sobrescreva campos com `over`)."""
    row = {
        "id": f"{task_type}-{n:02d}", "task_type": task_type, "lang": "pt-BR",
        "messages": [{"role": "user", "content": "Quanto é 2 + 2? Responda só com o número."}],
        "params": {"temperature": 0, "max_tokens": 20},
    }
    if task_type == "summarization":
        row.update(reference_answer="Um resumo curto.", key_points=["ponto um", "ponto dois"])
    else:
        row["check"] = {"type": "numeric", "spec": {"value": 4}}
    row.update(over)
    return row


def write_rows(directory: Path, rows: list[dict], name="a.jsonl") -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    text = "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows)
    with open(directory / name, "w", encoding="utf-8", newline="\n") as f:  # LF em qualquer sistema
        f.write(text)
    return directory


def problems_of(directory: Path, **kw) -> list[str]:
    with pytest.raises(vd.DatasetError) as e:
        vd.load_dataset(directory, context_window=CONTEXT, expect_counts=False, **kw)
    return e.value.problems


# --------------------------------------------------------------------------- dataset real
def test_real_dataset_is_valid_with_6_categories_of_8():
    ds = vd.load_dataset()
    assert len(ds.prompts) == 48
    assert Counter(p.task_type for p in ds.prompts) == {t: 8 for t in TASK_TYPES}
    ids = [p.id for p in ds.prompts]
    assert len(set(ids)) == 48
    assert all(p.lang == "pt-BR" and p.params.temperature == 0 for p in ds.prompts)


def test_real_dataset_only_uses_checks_and_references_as_specified():
    for p in vd.load_dataset().prompts:
        if p.task_type == "summarization":
            assert p.check is None and p.reference_answer and p.key_points
        else:
            assert p.check is not None, p.id
    types = {p.check.type for p in vd.load_dataset().prompts if p.check}
    assert types == {"json_schema", "exact", "numeric", "label_in", "constraints"}


def test_real_dataset_prompts_fit_the_local_context():
    window = vd.default_context_window()
    assert window == CONTEXT
    for p in vd.load_dataset().prompts:
        tokens = estimate_tokens(p.request_body())
        assert tokens <= vd.MAX_PROMPT_TOKENS and tokens + p.params.max_tokens <= window, p.id


def test_cli_accepts_the_real_dataset(capsys):
    assert vd.main([]) == 0
    out = capsys.readouterr().out
    assert out.startswith("OK: 48 prompts") and "hash" in out


def test_dataset_hash_is_stable_and_ignores_line_endings(tmp_path):
    rows = [good()]
    a = write_rows(tmp_path / "a", rows)
    b = write_rows(tmp_path / "b", rows)
    b_file = b / "a.jsonl"
    b_file.write_bytes(b_file.read_bytes().replace(b"\n", b"\r\n"))
    assert vd.dataset_hash(a) == vd.dataset_hash(b) and len(vd.dataset_hash(a)) == 64
    write_rows(tmp_path / "c", [good(n=2)])
    assert vd.dataset_hash(tmp_path / "c") != vd.dataset_hash(a)


def test_token_estimate_matches_the_gateway():
    for body in ({"messages": [{"role": "user", "content": "x" * 400}]},
                 {"messages": [{"role": "system", "content": "abc"}, {"role": "user", "content": "defgh"}]},
                 {"messages": []}):
        assert estimate_tokens(body) == gateway_estimate_tokens(body)


# --------------------------------------------------------------------------- aceita
def test_accepts_a_minimal_valid_dataset(tmp_path):
    rows = [good("reasoning", 1), good("summarization", 1)]
    ds = vd.load_dataset(write_rows(tmp_path, rows), context_window=CONTEXT, expect_counts=False)
    assert [p.id for p in ds.prompts] == ["reasoning-01", "summarization-01"]
    assert ds.by_id()["reasoning-01"].request_body()["max_tokens"] == 20


@pytest.mark.parametrize("check", [
    {"type": "json_schema", "spec": {"schema": {"type": "object", "properties": {"a": {"type": "string"}}, "required": ["a"]},
                                      "expected": {"a": "x"}}},
    {"type": "exact", "spec": {"value": "Clara"}},
    {"type": "regex", "spec": {"pattern": r"^\d+$", "flags": "i"}},
    {"type": "numeric", "spec": {"value": 3.5, "tolerance": 0.1}},
    {"type": "label_in", "spec": {"labels": ["a", "b"], "expected": "b"}},
    {"type": "constraints", "spec": {"rules": [{"kind": "max_words", "value": 5}, {"kind": "forbidden_words", "value": ["x"]},
                                              {"kind": "lowercase", "value": True}]}},
])
def test_accepts_every_check_type(tmp_path, check):
    assert vd.load_dataset(write_rows(tmp_path, [good(check=check)]), context_window=CONTEXT, expect_counts=False)


# --------------------------------------------------------------------------- rejeita
def test_rejects_duplicate_id(tmp_path):
    problems = problems_of(write_rows(tmp_path, [good(), good()]))
    assert any("id duplicado" in p and "reasoning-01" in p for p in problems)


def test_rejects_duplicate_id_across_files(tmp_path):
    write_rows(tmp_path, [good()], "a.jsonl")
    write_rows(tmp_path, [good()], "b.jsonl")
    assert any("id duplicado" in p for p in problems_of(tmp_path))


@pytest.mark.parametrize("field", ["id", "task_type", "lang", "messages", "params"])
def test_rejects_missing_required_field(tmp_path, field):
    row = good()
    del row[field]
    assert problems_of(write_rows(tmp_path, [row]))


def test_rejects_missing_check_and_missing_summarization_reference(tmp_path):
    no_check = good()
    del no_check["check"]
    no_ref = good("summarization", 1)
    del no_ref["reference_answer"]
    no_points = good("summarization", 2, key_points=["só um"])
    problems = problems_of(write_rows(tmp_path, [no_check, no_ref, no_points]))
    assert any("exige check" in p for p in problems)
    assert any("exige reference_answer" in p for p in problems)
    assert any("key_points" in p for p in problems)


@pytest.mark.parametrize("check,fragment", [
    ({"type": "fuzzy", "spec": {}}, "check.type"),
    ({"type": "numeric", "spec": {}}, "obrigatório"),
    ({"type": "numeric", "spec": {"value": "4"}}, "número"),
    ({"type": "numeric", "spec": {"value": 4, "extra": 1}}, "desconhecido"),
    ({"type": "regex", "spec": {"pattern": "("}}, "pattern inválido"),
    ({"type": "regex", "spec": {"pattern": "a", "flags": "x"}}, "flags"),
    ({"type": "exact", "spec": {"value": " "}}, "não vazio"),
    ({"type": "label_in", "spec": {"labels": ["a", "b"], "expected": "c"}}, "um dos labels"),
    ({"type": "label_in", "spec": {"labels": ["a"], "expected": "a"}}, "pelo menos 2"),
    ({"type": "label_in", "spec": {"labels": ["a", "A"], "expected": "a"}}, "repetidos"),
    ({"type": "constraints", "spec": {"rules": [{"kind": "inventada", "value": 1}]}}, "kind desconhecido"),
    ({"type": "constraints", "spec": {"rules": [{"kind": "max_words", "value": "5"}]}}, "deve ser int"),
    ({"type": "constraints", "spec": {"rules": [{"kind": "max_words", "value": -1}]}}, ">= 0"),
    ({"type": "constraints", "spec": {"rules": [{"kind": "forbidden_words", "value": []}]}}, "lista não vazia"),
    ({"type": "constraints", "spec": {"rules": []}}, "não vazia"),
    ({"type": "json_schema", "spec": {"schema": {"type": "array"}}}, "type=object"),
    ({"type": "json_schema", "spec": {"schema": {"type": "object", "properties": {"a": {"type": "texto"}}}}}, "type inválido"),
    ({"type": "json_schema", "spec": {"schema": {"type": "object", "properties": {"a": {"type": "string"}},
                                                 "oneOf": []}}}, "fora do subconjunto"),
    ({"type": "json_schema", "spec": {"schema": {"type": "object", "properties": {"a": {"type": "string"}}},
                                      "expected": {"b": 1}}}, "não existe"),
    ({"type": "json_schema", "spec": {"schema": {"type": "object", "properties": {"a": {"type": "string"}},
                                                 "required": ["b"]}}}, "sem definição"),
])
def test_rejects_invalid_check(tmp_path, check, fragment):
    problems = problems_of(write_rows(tmp_path, [good(check=check)]))
    assert any(fragment in p for p in problems), problems


def test_rejects_prompt_over_the_prompt_budget_and_over_the_context(tmp_path):
    long = good(messages=[{"role": "user", "content": "palavra " * 500}])  # ~1000 tokens
    problems = problems_of(write_rows(tmp_path, [long]))
    assert any("máximo 800" in p for p in problems)
    # cabe nos 800, mas junto com max_tokens estoura um contexto menor
    near = good(messages=[{"role": "user", "content": "palavra " * 300}])
    write_rows(tmp_path / "near", [near])
    with pytest.raises(vd.DatasetError) as e:
        vd.load_dataset(tmp_path / "near", context_window=500, expect_counts=False)
    assert any("passa do contexto de 500" in p for p in e.value.problems)


def test_rejects_sensitive_patterns_without_echoing_them(tmp_path):
    key = "sk-" + "ant-" + "falsa1234567890abcdef"  # montada em runtime
    email = "fulano" + "@" + "exemplo.com"
    rows = [
        good(n=1, messages=[{"role": "user", "content": f"A chave é {key}"}]),
        good(n=2, messages=[{"role": "user", "content": f"Escreva para {email}"}]),
        good(n=3, messages=[{"role": "user", "content": "O CPF é 123.456.789-09"}]),
        good(n=4, notes="ligue para (11) 91234-5678"),
        good(n=5, notes="arquivo em " + "C:" + "\\Users\\alguem\\x\\y"),
    ]
    problems = problems_of(write_rows(tmp_path, rows))
    assert len(problems) == 5 and all("padrão sensível" in p for p in problems)
    joined = "\n".join(problems)
    assert key not in joined and email not in joined and "123.456.789" not in joined


def test_sensitive_scan_covers_reference_key_points_and_check(tmp_path):
    email = "x" + "@" + "exemplo.com"
    rows = [
        good("summarization", 1, reference_answer=f"resumo de {email}"),
        good("summarization", 2, key_points=["ok", f"contato {email}"]),
        good(n=3, check={"type": "exact", "spec": {"value": email}}),
    ]
    assert len(problems_of(write_rows(tmp_path, rows))) == 3


def test_rejects_bad_structure(tmp_path):
    rows = [
        good(n=1, id="classification-01"),                         # id não combina com a categoria
        good(n=2, id="Reasoning_2"),                                # formato do id
        good(n=3, params={"temperature": 0.7, "max_tokens": 20}),   # não determinístico
        good(n=4, params={"temperature": 0, "max_tokens": 0}),
        good(n=5, lang="en"),
        good(n=6, messages=[{"role": "assistant", "content": "oi"}]),  # última mensagem não é do usuário
        good(n=7, key_points=["a", "b"]),                           # key_points fora de summarization
        good(n=8, extra_field=1),                                   # campo desconhecido
    ]
    problems = problems_of(write_rows(tmp_path, rows))
    assert len(problems) == 8


def test_invalid_json_line_and_empty_directory(tmp_path):
    (tmp_path / "a.jsonl").write_text("{não é json\n", encoding="utf-8")
    assert any("JSON inválido" in p and "a.jsonl:1" in p for p in problems_of(tmp_path))
    with pytest.raises(vd.DatasetError, match="nenhum arquivo"):
        vd.load_dataset(tmp_path / "vazio", context_window=CONTEXT)


def test_expected_counts_are_enforced_by_default(tmp_path):
    write_rows(tmp_path, [good()])
    with pytest.raises(vd.DatasetError) as e:
        vd.load_dataset(tmp_path, context_window=CONTEXT)
    assert any("categoria extraction: 0 prompts (esperado 8)" in p for p in e.value.problems)


def test_cli_reports_every_problem_and_exits_1(tmp_path, capsys):
    write_rows(tmp_path, [good(), good(), good(n=2, params={"temperature": 1, "max_tokens": 5})])
    assert vd.main(["--dir", str(tmp_path)]) == 1
    err = capsys.readouterr().err
    assert "DATASET INVÁLIDO" in err and "id duplicado" in err and "temperature" in err


def test_schema_model_is_strict_about_unknown_fields():
    with pytest.raises(ValidationError):
        EvalPrompt.model_validate(copy.deepcopy(good(unknown=1)))


# --------------------------------------------------------------------------- git
def check_ignored(path: str) -> bool:
    return subprocess.run(["git", "check-ignore", "-q", path], cwd=REPO_ROOT).returncode == 0


def test_dataset_is_versioned_but_results_and_baseline_are_ignored():
    assert not check_ignored("eval/prompts/extraction.jsonl")
    assert check_ignored("eval/results/run1/ollama_qwen.jsonl")
    assert check_ignored("eval/results/run1/meta.json")
    assert check_ignored("eval/baseline/extraction-01.json")
    assert check_ignored("outro.jsonl")  # *.jsonl segue ignorado fora do dataset
