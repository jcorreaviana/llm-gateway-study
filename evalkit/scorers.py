"""Scorers determinísticos da avaliação (Fase 3). Sem rede, sem modelo, sem Anthropic.

Cada scorer devolve um `Score` (`score` 0..1, `passed`, `details`) e NUNCA levanta exceção para entrada
estranha (resposta vazia, tipo errado, lixo): o erro vira score 0 com `details.scorer_error`.

Obediência de formato separada de correção: o scorer é tolerante (tira cercas de código, prosa antes e
depois, espaços) para medir se o conteúdo está certo, e registra em `details.format_clean` se a resposta
JÁ vinha no formato pedido, sem precisar de limpeza. `passed` depende só do conteúdo.

Erro do runner (HTTP != 200, timeout, `error` preenchido) e resposta vazia valem score 0 e são marcados
com `errored: true`: as análises de qualidade os contam à parte (`errors`), nunca como "resposta errada".

A semântica de cada `check` segue o docstring de `evalkit/schema.py`.
"""

from __future__ import annotations

import json
import math
import re
import unicodedata
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from evalkit.embedding import Embedder, EmbeddingUnavailable, cosine
from evalkit.schema import EvalPrompt

PASS_THRESHOLD_SUMMARY = 0.7
KEY_POINT_COVERED = 0.6  # fração dos termos de conteúdo de um ponto-chave que precisa aparecer
SUMMARY_WEIGHTS = {"coverage": 0.6, "similarity": 0.3, "length": 0.1}


@dataclass
class Score:
    score: float
    passed: bool
    details: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {"score": round(self.score, 4), "passed": self.passed, "details": self.details}


# --------------------------------------------------------------------------- normalização de texto
FENCE_RE = re.compile(r"```[^\n`]*\n?(.*?)```", re.DOTALL)
WORD_RE = re.compile(r"\w+(?:[-'’]\w+)*")
ITEM_RE = re.compile(r"^\s*(?:[-*•]|\d+[.)])\s+")


def as_text(value: Any) -> str:
    """Qualquer coisa vira texto (nunca levanta)."""
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    try:
        return json.dumps(value, ensure_ascii=False)
    except (TypeError, ValueError):
        return str(value)


def strip_accents(text: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFD", text) if unicodedata.category(c) != "Mn")


def fold(text: str) -> str:
    """Sem acento, sem caixa e com espaços normalizados (para comparar conteúdo)."""
    return " ".join(strip_accents(text).casefold().split())


def unfence(text: str) -> tuple[str, bool]:
    """Se a resposta é UM bloco de código cercado (```), devolve o miolo. O bool diz se houve cerca."""
    stripped = text.strip()
    m = FENCE_RE.fullmatch(stripped)
    if m:
        return m.group(1).strip(), True
    return stripped, False


def extract_json(text: str) -> tuple[Any, bool, str | None]:
    """Primeiro objeto ou array JSON válido do texto, tolerando cercas e prosa antes/depois.
    Devolve (valor, format_clean, erro). `format_clean` é True só se o texto inteiro (sem espaços) já era
    o JSON, sem cerca nem prosa."""
    raw = as_text(text)
    stripped = raw.strip()
    try:
        return json.loads(stripped), True, None
    except ValueError:
        pass
    decoder = json.JSONDecoder()
    for m in re.finditer(r"[{\[]", raw):
        try:
            value, _ = decoder.raw_decode(raw, m.start())
        except ValueError:
            continue
        if isinstance(value, dict | list):
            return value, False, None
    return None, False, "nenhum objeto ou array JSON válido na resposta"


# --------------------------------------------------------------------------- json_schema
def _type_ok(value: Any, expected: str) -> bool:
    if expected == "string":
        return isinstance(value, str)
    if expected == "boolean":
        return isinstance(value, bool)
    if expected == "integer":
        return (isinstance(value, int) and not isinstance(value, bool)) or (
            isinstance(value, float) and math.isfinite(value) and value == int(value)
        )
    if expected == "number":
        return isinstance(value, int | float) and not isinstance(value, bool)
    if expected == "array":
        return isinstance(value, list)
    if expected == "object":
        return isinstance(value, dict)
    if expected == "null":
        return value is None
    return False


def schema_violations(value: Any, schema: dict[str, Any], path: str = "$") -> list[tuple[str, str]]:
    """Violações (caminho, motivo) do subconjunto suportado: type, properties, required, enum, items,
    additionalProperties."""
    out: list[tuple[str, str]] = []
    t = schema.get("type")
    if t is not None and not _type_ok(value, t):
        return [(path, f"tipo esperado {t}, veio {type(value).__name__}")]
    if "enum" in schema and value not in schema["enum"]:
        out.append((path, "valor fora do enum"))
    if isinstance(value, dict):
        props = schema.get("properties", {})
        for name in schema.get("required", []):
            if name not in value:
                out.append((f"{path}.{name}", "campo obrigatório ausente"))
        for name, sub in props.items():
            if name in value:
                out.extend(schema_violations(value[name], sub, f"{path}.{name}"))
        if schema.get("additionalProperties") is False:
            out.extend((f"{path}.{k}", "campo não permitido") for k in value if k not in props)
    elif isinstance(value, list) and "items" in schema:
        for i, item in enumerate(value):
            out.extend(schema_violations(item, schema["items"], f"{path}[{i}]"))
    return out


def values_equal(got: Any, want: Any) -> bool:
    """Textos com strip/casefold, números por valor (bool não é número), listas e objetos recursivamente."""
    if isinstance(want, bool) or isinstance(got, bool):
        return isinstance(want, bool) and isinstance(got, bool) and got == want
    if isinstance(want, str):
        return isinstance(got, str) and got.strip().casefold() == want.strip().casefold()
    if isinstance(want, int | float):
        return isinstance(got, int | float) and math.isclose(got, want, rel_tol=1e-9, abs_tol=1e-9)
    if isinstance(want, list):
        return (isinstance(got, list) and len(got) == len(want)
                and all(values_equal(g, w) for g, w in zip(got, want, strict=True)))
    if isinstance(want, dict):
        return isinstance(got, dict) and all(k in got and values_equal(got[k], v) for k, v in want.items())
    return got == want


def _field_paths(schema: dict[str, Any], expected: Any, prefix: tuple[str, ...] = ()) -> list[tuple[str, ...]]:
    """Campos pontuados: folhas de `expected` + campos `required` (recursivamente). Um objeto sem filhos
    pontuáveis conta como folha."""
    props = schema.get("properties", {})
    names = list(dict.fromkeys([*schema.get("required", []), *(expected.keys() if isinstance(expected, dict) else [])]))
    names = [n for n in names if n in props]
    paths: list[tuple[str, ...]] = []
    for n in names:
        sub_expected = expected.get(n) if isinstance(expected, dict) else None
        sub_schema = props[n]
        children = _field_paths(sub_schema, sub_expected, (*prefix, n)) if sub_schema.get("type") == "object" else []
        paths.extend(children or [(*prefix, n)])
    return paths


def _under(violation_path: str, dotted: str) -> bool:
    """A violação `$.a.b[0]` está no campo `a.b` (ou abaixo dele)?"""
    base = "$." + dotted
    return violation_path == base or violation_path.startswith((base + ".", base + "["))


def _dig(value: Any, path: tuple[str, ...]) -> tuple[bool, Any]:
    for key in path:
        if not isinstance(value, dict) or key not in value:
            return False, None
        value = value[key]
    return True, value


def _sub_schema(schema: dict[str, Any], path: tuple[str, ...]) -> dict[str, Any]:
    for key in path:
        schema = schema.get("properties", {}).get(key, {})
    return schema


def score_json_schema(text: str, spec: dict[str, Any]) -> Score:
    value, clean, err = extract_json(text)
    if err:
        return Score(0.0, False, {"format_clean": False, "json_found": False, "error": err})
    schema, expected = spec["schema"], spec.get("expected")
    details: dict[str, Any] = {"format_clean": clean, "json_found": True}
    if not isinstance(value, dict):
        details["error"] = f"esperado um objeto JSON, veio {type(value).__name__}"
        return Score(0.0, False, details)
    violations = schema_violations(value, schema)
    paths = _field_paths(schema, expected)
    wrong: dict[str, str] = {}
    for path in paths:
        dotted = ".".join(path)
        present, got = _dig(value, path)
        if not present:
            wrong[dotted] = "ausente"
            continue
        bad = [m for p, m in violations if _under(p, dotted)]
        if bad:
            wrong[dotted] = bad[0]
            continue
        _, want = _dig(expected, path) if expected is not None else (False, None)
        want_known = expected is not None and _dig(expected, path)[0]
        if want_known and not values_equal(got, want):
            wrong[dotted] = f"valor diferente (esperado {want!r}, veio {got!r})"
    # violações fora dos campos pontuados (enum de campo opcional, campo extra proibido...) também contam
    covered = {".".join(p) for p in paths}
    extra = [(p, m) for p, m in violations if not any(_under(p, c) for c in covered)]
    for p, m in extra:
        wrong[p.removeprefix("$.")] = m
    total = len(paths) + len(extra)
    correct = len(paths) - sum(1 for p in paths if ".".join(p) in wrong)
    score = correct / total if total else 0.0
    details.update(fields_total=total, fields_correct=correct, wrong_fields=wrong)
    return Score(score, not wrong and total > 0, details)


# --------------------------------------------------------------------------- exact
@dataclass(frozen=True)
class ExactOptions:
    """Normalização configurável do `exact`. Padrão = semântica do schema (strip, casefold, pontuação final)."""

    casefold: bool = True
    accents: bool = False  # True remove acentos
    punctuation: str = "trailing"  # "none" | "trailing" | "all"
    whitespace: bool = True  # colapsa espaços internos


DEFAULT_EXACT = ExactOptions()


def normalize_exact(text: str, opts: ExactOptions = DEFAULT_EXACT) -> str:
    out = text.strip()
    if opts.accents:
        out = strip_accents(out)
    if opts.casefold:
        out = out.casefold()
    if opts.punctuation == "all":
        out = "".join(c for c in out if not unicodedata.category(c).startswith("P"))
    elif opts.punctuation == "trailing":
        out = out.rstrip(".,;:!?…\"'”’)] \t\n").lstrip("\"'“‘([ ")
    if opts.whitespace:
        out = " ".join(out.split())
    return out.strip()


def score_exact(text: str, spec: dict[str, Any], opts: ExactOptions = DEFAULT_EXACT) -> Score:
    body, fenced = unfence(as_text(text))
    got, want = normalize_exact(body, opts), normalize_exact(spec["value"], opts)
    ok = got == want
    clean = (not fenced) and body.strip() == spec["value"].strip()
    return Score(1.0 if ok else 0.0, ok, {"format_clean": clean, "normalized": got[:120], "expected": want})


# --------------------------------------------------------------------------- regex
def score_regex(text: str, spec: dict[str, Any]) -> Score:
    from evalkit.schema import REGEX_FLAGS

    flags = sum(REGEX_FLAGS[f] for f in spec.get("flags", ""))
    body, fenced = unfence(as_text(text))
    m = re.search(spec["pattern"], body, flags)
    ok = m is not None
    details: dict[str, Any] = {"format_clean": not fenced, "pattern": spec["pattern"]}
    if m:
        details["match"] = m.group(0)[:80]
    return Score(1.0 if ok else 0.0, ok, details)


# --------------------------------------------------------------------------- numeric
NUMBER_RE = re.compile(r"[-−–]?\d[\d.,]*\d|[-−–]?\d")


def parse_number(token: str) -> float | None:
    """Número em formato PT-BR ou internacional: `1.234,5`, `1,5`, `1.234`, `3.5`, `1,234.5`, `-7`.
    Regras: com vírgula e ponto, o ÚLTIMO separador é o decimal; só vírgula: uma = decimal, várias =
    milhar; só ponto: grupos de 3 dígitos (`1.234`, `1.234.567`) são milhar, senão decimal (`3.5`, `0.500`)."""
    t = token.replace("−", "-").replace("–", "-")
    neg = t.startswith("-")
    t = t.lstrip("-")
    try:
        if "," in t and "." in t:
            dec = "," if t.rfind(",") > t.rfind(".") else "."
            thousands = "." if dec == "," else ","
            t = t.replace(thousands, "").replace(dec, ".")
        elif "," in t:
            t = t.replace(",", ".") if t.count(",") == 1 else t.replace(",", "")
        elif "." in t:
            int_part = t.split(".")[0]
            if re.fullmatch(r"\d{1,3}(\.\d{3})+", t) and int_part != "0":
                t = t.replace(".", "")
            elif t.count(".") > 1:
                return None
        value = float(t)
    except ValueError:
        return None
    return -value if neg else value


def last_number(text: str) -> tuple[float | None, str | None]:
    for m in reversed(list(NUMBER_RE.finditer(text))):
        value = parse_number(m.group(0))
        if value is not None:
            return value, m.group(0)
    return None, None


def score_numeric(text: str, spec: dict[str, Any]) -> Score:
    body, fenced = unfence(as_text(text))
    value, token = last_number(body)
    if value is None:
        return Score(0.0, False, {"format_clean": False, "number_found": False, "expected": spec["value"]})
    tol = float(spec.get("tolerance", 0))
    ok = abs(value - float(spec["value"])) <= tol + 1e-9
    only_number = (not fenced) and body.strip() == token
    return Score(1.0 if ok else 0.0, ok, {"format_clean": only_number, "number_found": True, "parsed": value,
                                          "expected": spec["value"], "tolerance": tol})


# --------------------------------------------------------------------------- label_in
def _label_form(text: str) -> str:
    return fold(re.sub(r"[_\-]+", " ", text))


def labels_found(text: str, labels: list[str]) -> list[str]:
    """Rótulos que aparecem como palavra inteira (sem acento, sem caixa; `_`/`-` valem como espaço)."""
    hay = _label_form(text)
    found = []
    for label in labels:
        pattern = r"(?<!\w)" + re.escape(_label_form(label)) + r"(?!\w)"
        if re.search(pattern, hay):
            found.append(label)
    return found


def score_label_in(text: str, spec: dict[str, Any]) -> Score:
    body, fenced = unfence(as_text(text))
    found = labels_found(body, spec["labels"])
    clean = (not fenced) and body.strip().casefold() in {x.strip().casefold() for x in spec["labels"]}
    details: dict[str, Any] = {"format_clean": clean, "labels_found": found, "expected": spec["expected"]}
    if len(found) != 1:
        details["error"] = "nenhum rótulo na resposta" if not found else "mais de um rótulo diferente na resposta"
        return Score(0.0, False, details)
    ok = found[0] == spec["expected"]
    return Score(1.0 if ok else 0.0, ok, details)


# --------------------------------------------------------------------------- constraints
def count_words(text: str) -> int:
    return len(WORD_RE.findall(text))


def count_sentences(text: str) -> int:
    return len([s for s in re.split(r"(?<=[.!?…])\s+", text.strip()) if s.strip()])


def _items(text: str) -> int:
    return sum(1 for line in text.splitlines() if ITEM_RE.match(line))


def _lines(text: str) -> int:
    return sum(1 for line in text.splitlines() if line.strip())


def _has_word(text: str, word: str) -> bool:
    return re.search(r"(?<!\w)" + re.escape(word) + r"(?!\w)", text, re.IGNORECASE) is not None


RULES: dict[str, Callable[[str, Any], tuple[bool, Any]]] = {
    "max_chars": lambda t, v: (len(t) <= v, len(t)),
    "min_chars": lambda t, v: (len(t) >= v, len(t)),
    "max_words": lambda t, v: (count_words(t) <= v, count_words(t)),
    "min_words": lambda t, v: (count_words(t) >= v, count_words(t)),
    "n_items": lambda t, v: (_items(t) == v, _items(t)),
    "n_lines": lambda t, v: (_lines(t) == v, _lines(t)),
    "n_sentences": lambda t, v: (count_sentences(t) == v, count_sentences(t)),
    "forbidden_words": lambda t, v: (not (hit := [w for w in v if _has_word(t, w)]), hit),
    "required_words": lambda t, v: (not (miss := [w for w in v if not _has_word(t, w)]), miss),
    "starts_with": lambda t, v: (t.startswith(v), t[: len(v)]),
    "ends_with": lambda t, v: (t.endswith(v), t[-len(v):] if v else ""),
    "lowercase": lambda t, v: ((t == t.lower()) == v, t == t.lower()),
    "no_newline": lambda t, v: (("\n" not in t) == v, "\n" not in t),
}


def score_constraints(text: str, spec: dict[str, Any]) -> Score:
    body, fenced = unfence(as_text(text))
    results = []
    for rule in spec["rules"]:
        ok, observed = RULES[rule["kind"]](body, rule["value"])
        results.append({"kind": rule["kind"], "value": rule["value"], "passed": bool(ok), "observed": observed})
    passed_n = sum(r["passed"] for r in results)
    score = passed_n / len(results) if results else 0.0
    return Score(score, bool(results) and passed_n == len(results), {"format_clean": not fenced, "rules": results})


# --------------------------------------------------------------------------- summarization
STOPWORDS = frozenset(fold(w) for w in (
    "a o as os um uma uns umas de do da dos das em no na nos nas por para com sem sobre ao aos à às e ou mas que "
    "se ser foi sao são tem têm ha há mais menos muito pouco ate até entre como seu sua seus suas esta este "
    "isso essa esse ja já nao não pelo pela pelos pelas cada dia"
).split())


def content_terms(text: str) -> list[str]:
    terms = []
    for tok in re.findall(r"\w+", fold(text)):
        if tok in STOPWORDS or (len(tok) < 3 and not tok.isdigit()):
            continue
        terms.append(tok)
    return terms


def _term_in(term: str, hay: set[str]) -> bool:
    if term in hay:
        return True
    if len(term) >= 5:  # flexão (plural, gênero, conjugação): mesmo radical de 5 letras
        return any(len(h) >= 5 and h[:5] == term[:5] for h in hay)
    return False


def key_point_coverage(text: str, key_points: list[str]) -> tuple[float, list[dict[str, Any]]]:
    hay = set(re.findall(r"\w+", fold(text)))
    per_point = []
    covered = 0
    for kp in key_points:
        terms = content_terms(kp)
        hits = [t for t in terms if _term_in(t, hay)]
        recall = len(hits) / len(terms) if terms else 0.0
        ok = recall >= KEY_POINT_COVERED
        covered += ok
        per_point.append({"key_point": kp, "recall": round(recall, 3), "covered": ok})
    return (covered / len(key_points) if key_points else 0.0), per_point


_NUM_WORDS = {"uma": 1, "um": 1, "duas": 2, "dois": 2, "tres": 3, "quatro": 4, "cinco": 5, "seis": 6}


def size_limit(prompt_text: str) -> dict[str, Any] | None:
    """Limite de tamanho pedido no enunciado ("até 2 frases", "uma frase", "3 tópicos", "4 passos")."""
    m = re.search(r"(até\s+|em\s+)?(\d+|uma|um|duas|dois|tr[eê]s|quatro|cinco|seis)\s+(frases?|t[óo]picos?|passos?)",
                  prompt_text, re.IGNORECASE)
    if not m:
        return None
    raw = fold(m.group(2))
    n = int(raw) if raw.isdigit() else _NUM_WORDS.get(raw)
    if n is None:
        return None
    unit = "frases" if m.group(3).lower().startswith("frase") else "itens"
    return {"unit": unit, "n": n, "exact": not (m.group(1) and fold(m.group(1)).startswith("ate"))}


def length_ok(text: str, prompt_text: str, truncated: bool) -> tuple[bool, dict[str, Any]]:
    limit = size_limit(prompt_text)
    info: dict[str, Any] = {"limit": limit, "truncated": truncated}
    if truncated:
        info["reason"] = "resposta cortada por max_tokens"
        return False, info
    if limit is None:
        return True, info
    # itens/passos: só as linhas de lista contam (uma frase de abertura não é um passo); sem lista, vale a linha
    got = count_sentences(text) if limit["unit"] == "frases" else (_items(text) or _lines(text))
    info["observed"] = got
    return (got <= limit["n"] if not limit["exact"] or limit["unit"] == "frases" else got == limit["n"]), info


def score_summarization(prompt: EvalPrompt, text: str, embedder: Embedder | None, *, truncated: bool = False) -> Score:
    body, fenced = unfence(as_text(text))
    coverage, per_point = key_point_coverage(body, prompt.key_points or [])
    ok_len, len_info = length_ok(body, prompt.messages[-1].content, truncated)
    parts = {"coverage": coverage, "length": 1.0 if ok_len else 0.0}
    details: dict[str, Any] = {"format_clean": not fenced, "key_points": per_point, "length": len_info,
                               "coverage": round(coverage, 4)}
    sim = None
    if embedder is None:
        details["embedding"] = "unavailable"
    else:
        try:
            ref_vec, ans_vec = embedder.embed([prompt.reference_answer or "", body])
            sim = max(0.0, min(1.0, cosine(ref_vec, ans_vec)))
            details["embedding"] = "ok"
            details["similarity"] = round(sim, 4)
            parts["similarity"] = sim
        except Exception as e:  # noqa: BLE001 - o embedder é injetável; nada pode derrubar o run
            reason = str(e) if isinstance(e, EmbeddingUnavailable) else type(e).__name__
            details["embedding"] = f"unavailable ({reason})"
    weights = {k: SUMMARY_WEIGHTS[k] for k in parts}
    score = sum(parts[k] * w for k, w in weights.items()) / sum(weights.values())
    details["weights"] = {k: round(w / sum(weights.values()), 4) for k, w in weights.items()}
    return Score(score, score >= PASS_THRESHOLD_SUMMARY, details)


# --------------------------------------------------------------------------- ponto de entrada
SCORERS: dict[str, Callable[[str, dict[str, Any]], Score]] = {
    "json_schema": score_json_schema,
    "exact": score_exact,
    "regex": score_regex,
    "numeric": score_numeric,
    "label_in": score_label_in,
    "constraints": score_constraints,
}


def classify_error(record: dict[str, Any]) -> str | None:
    """Motivo pelo qual o resultado do runner não é uma resposta avaliável, ou None."""
    status = record.get("status")
    if record.get("error"):
        return "timeout_or_connection" if status == 0 else "runner_error"
    if status != 200:
        return "http_error"
    if not as_text(record.get("response")).strip():
        return "empty_response"
    return None


def score_record(prompt: EvalPrompt, record: dict[str, Any], embedder: Embedder | None = None) -> dict[str, Any]:
    """Pontua um resultado do runner. Devolve `{score, passed, errored, error_kind, details}`; nunca levanta."""
    try:
        kind = classify_error(record)
        if kind:
            return {"score": 0.0, "passed": False, "errored": True, "error_kind": kind,
                    "details": {"error": as_text(record.get("error"))[:200] or kind}}
        text = as_text(record.get("response"))
        truncated = record.get("finish_reason") == "length"
        if prompt.check is not None:
            result = SCORERS[prompt.check.type](text, prompt.check.spec)
        else:
            result = score_summarization(prompt, text, embedder, truncated=truncated)
        out = result.as_dict()
        out["details"]["truncated"] = truncated
        return {**out, "errored": False, "error_kind": None}
    except Exception as e:  # noqa: BLE001 - contrato: entrada estranha nunca derruba o scoring
        return {"score": 0.0, "passed": False, "errored": False, "error_kind": None,
                "details": {"scorer_error": f"{type(e).__name__}: {e}"[:200]}}
