"""Schema do dataset de avaliação (Fase 3).

Cada linha de `eval/prompts/*.jsonl` é um `EvalPrompt`. A referência de qualidade é (a) um `check`
verificável por regra, para tarefas de resposta objetiva, ou (b) uma `reference_answer` escrita à mão
(mais `key_points`), para tarefas abertas. Os scorers que executam os checks vêm no Marco 10; aqui só
se define e valida o FORMATO e a semântica pretendida de cada tipo.

Tipos de `check` (`spec`):

- `json_schema`: `{"schema": {...}, "expected": {...}}`. `schema` é um SUBCONJUNTO de JSON Schema
  (`type`, `properties`, `required`, `enum`, `items`, `additionalProperties`); `expected` (opcional) são
  valores que devem ser iguais, campo a campo (aninhado; só as chaves citadas são conferidas; textos
  comparados com `strip`/`casefold`, números por valor). A resposta deve ser um único objeto JSON.
- `exact`: `{"value": "texto"}`. Igualdade após `strip`, `casefold` e remoção de pontuação final.
- `regex`: `{"pattern": "...", "flags": "i"}` (flags opcionais: i, m, s). `re.search` na resposta.
- `numeric`: `{"value": 42, "tolerance": 0}`. Compara com o ÚLTIMO número da resposta (vírgula decimal ok).
- `label_in`: `{"labels": [...], "expected": "x"}`. A resposta deve ser exatamente um dos rótulos
  (após `strip`/`casefold`); acerta se for o `expected`.
- `constraints`: `{"rules": [{"kind": "...", "value": ...}]}`; todas as regras devem passar. Palavras
  (`forbidden_words`, `required_words`) são palavras inteiras, sem diferenciar maiúsculas de minúsculas;
  `max_*`/`min_*` são limites inclusivos.
"""

from __future__ import annotations

import re
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

TaskType = Literal["extraction", "classification", "reasoning", "instruction", "tool_use", "summarization"]
TASK_TYPES: tuple[str, ...] = ("extraction", "classification", "reasoning", "instruction", "tool_use", "summarization")
CheckType = Literal["json_schema", "exact", "regex", "numeric", "label_in", "constraints"]

ID_RE = re.compile(r"^[a-z_]+-\d{2}$")
JSON_TYPES = {"string", "integer", "number", "boolean", "array", "object", "null"}
SCHEMA_KEYS = {"type", "properties", "required", "enum", "items", "additionalProperties"}
REGEX_FLAGS = {"i": re.IGNORECASE, "m": re.MULTILINE, "s": re.DOTALL}

# kind -> tipo do `value`
RULE_KINDS: dict[str, type] = {
    "max_chars": int,
    "min_chars": int,
    "max_words": int,
    "min_words": int,
    "n_items": int,  # linhas que começam com marcador de lista ("- ", "* ", "1. ")
    "n_lines": int,  # linhas não vazias
    "n_sentences": int,
    "forbidden_words": list,
    "required_words": list,
    "starts_with": str,
    "ends_with": str,
    "lowercase": bool,
    "no_newline": bool,
}


class Message(BaseModel):
    model_config = ConfigDict(extra="forbid")

    role: Literal["system", "user", "assistant"]
    content: str = Field(min_length=1)


class Params(BaseModel):
    model_config = ConfigDict(extra="forbid")

    temperature: float = 0
    max_tokens: int = Field(gt=0, le=1024)

    @field_validator("temperature")
    @classmethod
    def _deterministic(cls, v: float) -> float:
        if v != 0:
            raise ValueError("temperature deve ser 0 (medição determinística)")
        return v


class Check(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: CheckType
    spec: dict[str, Any]

    @model_validator(mode="after")
    def _validate_spec(self) -> Check:
        _SPEC_VALIDATORS[self.type](self.spec)
        return self


# --------------------------------------------------------------------------- validação dos specs
def _only(spec: dict[str, Any], required: set[str], optional: frozenset[str] = frozenset()) -> None:
    missing = required - spec.keys()
    extra = spec.keys() - required - optional
    if missing:
        raise ValueError(f"spec sem campo(s) obrigatório(s): {', '.join(sorted(missing))}")
    if extra:
        raise ValueError(f"spec com campo(s) desconhecido(s): {', '.join(sorted(extra))}")


def _check_schema(schema: Any, path: str = "schema") -> None:
    if not isinstance(schema, dict):
        raise ValueError(f"{path} deve ser um objeto")
    unknown = schema.keys() - SCHEMA_KEYS
    if unknown:
        raise ValueError(f"{path} usa palavra(s) fora do subconjunto suportado: {', '.join(sorted(unknown))}")
    t = schema.get("type")
    if t is not None and t not in JSON_TYPES:
        raise ValueError(f"{path}.type inválido: {t!r}")
    props = schema.get("properties", {})
    if not isinstance(props, dict):
        raise ValueError(f"{path}.properties deve ser um objeto")
    for name, sub in props.items():
        _check_schema(sub, f"{path}.properties.{name}")
    required = schema.get("required", [])
    if not isinstance(required, list) or not all(isinstance(r, str) for r in required):
        raise ValueError(f"{path}.required deve ser uma lista de nomes")
    missing = [r for r in required if r not in props]
    if missing:
        raise ValueError(f"{path}.required cita campo(s) sem definição em properties: {', '.join(missing)}")
    if "items" in schema:
        _check_schema(schema["items"], f"{path}.items")
    if "enum" in schema and (not isinstance(schema["enum"], list) or not schema["enum"]):
        raise ValueError(f"{path}.enum deve ser uma lista não vazia")


def _check_expected(expected: Any, schema: dict[str, Any], path: str = "expected") -> None:
    if not isinstance(expected, dict):
        return
    props = schema.get("properties", {})
    for key, value in expected.items():
        if key not in props:
            raise ValueError(f"{path}.{key} não existe em schema.properties")
        _check_expected(value, props[key], f"{path}.{key}")


def _v_json_schema(spec: dict[str, Any]) -> None:
    _only(spec, {"schema"}, frozenset({"expected"}))
    schema = spec["schema"]
    _check_schema(schema)
    if schema.get("type") != "object" or not schema.get("properties"):
        raise ValueError("schema deve descrever um objeto (type=object) com properties")
    if "expected" in spec:
        if not isinstance(spec["expected"], dict) or not spec["expected"]:
            raise ValueError("expected deve ser um objeto não vazio")
        _check_expected(spec["expected"], schema)


def _v_exact(spec: dict[str, Any]) -> None:
    _only(spec, {"value"})
    if not isinstance(spec["value"], str) or not spec["value"].strip():
        raise ValueError("value deve ser um texto não vazio")


def _v_regex(spec: dict[str, Any]) -> None:
    _only(spec, {"pattern"}, frozenset({"flags"}))
    flags = spec.get("flags", "")
    if not isinstance(flags, str) or set(flags) - REGEX_FLAGS.keys():
        raise ValueError("flags aceita só i, m, s")
    pattern = spec["pattern"]
    if not isinstance(pattern, str) or not pattern:
        raise ValueError("pattern deve ser um texto não vazio")
    try:
        re.compile(pattern, sum(REGEX_FLAGS[f] for f in flags))
    except re.error as e:
        raise ValueError(f"pattern inválido: {e}") from e


def _is_number(v: Any) -> bool:
    return isinstance(v, int | float) and not isinstance(v, bool)


def _v_numeric(spec: dict[str, Any]) -> None:
    _only(spec, {"value"}, frozenset({"tolerance"}))
    if not _is_number(spec["value"]):
        raise ValueError("value deve ser um número")
    tol = spec.get("tolerance", 0)
    if not _is_number(tol) or tol < 0:
        raise ValueError("tolerance deve ser um número >= 0")


def _v_label_in(spec: dict[str, Any]) -> None:
    _only(spec, {"labels", "expected"})
    labels = spec["labels"]
    if not isinstance(labels, list) or len(labels) < 2 or not all(isinstance(x, str) and x.strip() for x in labels):
        raise ValueError("labels deve ter pelo menos 2 textos não vazios")
    folded = [x.strip().casefold() for x in labels]
    if len(set(folded)) != len(folded):
        raise ValueError("labels repetidos")
    if spec["expected"] not in labels:
        raise ValueError("expected deve ser um dos labels")


def _v_constraints(spec: dict[str, Any]) -> None:
    _only(spec, {"rules"})
    rules = spec["rules"]
    if not isinstance(rules, list) or not rules:
        raise ValueError("rules deve ser uma lista não vazia")
    for i, rule in enumerate(rules):
        if not isinstance(rule, dict) or rule.keys() != {"kind", "value"}:
            raise ValueError(f"rules[{i}] deve ter exatamente kind e value")
        kind, value = rule["kind"], rule["value"]
        if kind not in RULE_KINDS:
            raise ValueError(f"rules[{i}].kind desconhecido: {kind!r} (válidos: {', '.join(RULE_KINDS)})")
        expected_type = RULE_KINDS[kind]
        if not isinstance(value, expected_type) or (expected_type is int and isinstance(value, bool)):
            raise ValueError(f"rules[{i}].value de '{kind}' deve ser {expected_type.__name__}")
        if expected_type is int and value < 0:
            raise ValueError(f"rules[{i}].value de '{kind}' deve ser >= 0")
        if expected_type is list and (not value or not all(isinstance(w, str) and w for w in value)):
            raise ValueError(f"rules[{i}].value de '{kind}' deve ser uma lista não vazia de textos")


_SPEC_VALIDATORS = {
    "json_schema": _v_json_schema,
    "exact": _v_exact,
    "regex": _v_regex,
    "numeric": _v_numeric,
    "label_in": _v_label_in,
    "constraints": _v_constraints,
}


# --------------------------------------------------------------------------- prompt
class EvalPrompt(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    task_type: TaskType
    lang: Literal["pt-BR"]
    messages: list[Message] = Field(min_length=1)
    params: Params
    check: Check | None = None
    reference_answer: str | None = None
    key_points: list[str] | None = None
    notes: str | None = None

    @field_validator("id")
    @classmethod
    def _id_format(cls, v: str) -> str:
        if not ID_RE.match(v):
            raise ValueError("id deve seguir o formato <categoria>-NN (ex.: extraction-01)")
        return v

    @model_validator(mode="after")
    def _by_task_type(self) -> EvalPrompt:
        if not self.id.startswith(f"{self.task_type}-"):
            raise ValueError(f"id '{self.id}' não combina com task_type '{self.task_type}'")
        if self.messages[-1].role != "user":
            raise ValueError("a última mensagem deve ser do usuário")
        if self.task_type == "summarization":
            if not (self.reference_answer and self.reference_answer.strip()):
                raise ValueError("summarization exige reference_answer")
            if not self.key_points or not 2 <= len(self.key_points) <= 6 or not all(k.strip() for k in self.key_points):
                raise ValueError("summarization exige key_points (2 a 6 itens não vazios)")
        else:
            if self.check is None:
                raise ValueError(f"{self.task_type} exige check")
            if self.key_points is not None:
                raise ValueError("key_points só vale para summarization")
        if self.reference_answer is not None and not self.reference_answer.strip():
            raise ValueError("reference_answer vazia")
        return self

    def request_body(self) -> dict[str, Any]:
        """Corpo OpenAI-compatível enviado ao gateway."""
        return {
            "messages": [m.model_dump() for m in self.messages],
            "temperature": self.params.temperature,
            "max_tokens": self.params.max_tokens,
        }
