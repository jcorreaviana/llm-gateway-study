"""Validador do dataset de avaliação.

    python -m evalkit.validate_dataset [--dir eval/prompts]

Confere: JSON válido e campos por tipo (schema pydantic), ids únicos, estimativa de tokens dentro do
contexto dos modelos locais, nenhum padrão sensível (reusa `scripts/check_secrets.py`) e a composição
esperada (6 categorias x 8 prompts). Sai com 1 e lista TODOS os problemas, sem ecoar valores sensíveis.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path

from pydantic import ValidationError

from evalkit.schema import TASK_TYPES, EvalPrompt
from gateway.config import PROJECT_ROOT, load_config
from gateway.providers import estimate_tokens
from scripts.check_secrets import scan_line

PROMPTS_DIR = PROJECT_ROOT / "eval" / "prompts"
EVAL_CONFIG = PROJECT_ROOT / "config.eval.yaml"
PER_CATEGORY = 8
MAX_PROMPT_TOKENS = 800  # entrada estimada (caracteres/4); o contexto total inclui a resposta

# Além dos padrões do check_secrets: dados pessoais comuns que um dataset sintético não deve ter.
EXTRA_PATTERNS: dict[str, re.Pattern[str]] = {
    "e-mail": re.compile(r"[\w.+-]+@[\w-]+\.[\w.]+"),
    "CPF": re.compile(r"\b\d{3}\.\d{3}\.\d{3}-\d{2}\b"),
    "CNPJ": re.compile(r"\b\d{2}\.\d{3}\.\d{3}/\d{4}-\d{2}\b"),
    "telefone": re.compile(r"(?<!\d)(?:\+55\s?)?\(?\d{2}\)?\s?9?\d{4}-\d{4}(?!\d)"),
}


class DatasetError(Exception):
    """Dataset inválido; `problems` lista tudo o que foi encontrado."""

    def __init__(self, problems: list[str]) -> None:
        super().__init__("\n".join(problems))
        self.problems = problems


@dataclass
class Dataset:
    prompts: list[EvalPrompt] = field(default_factory=list)
    files: list[Path] = field(default_factory=list)

    def by_id(self) -> dict[str, EvalPrompt]:
        return {p.id: p for p in self.prompts}


def default_context_window() -> int:
    """Menor `context_window` entre os provedores da chain do config de avaliação."""
    cfg = load_config(EVAL_CONFIG)
    return min(cfg.provider(name).context_window for name in cfg.chain)


def prompt_texts(p: EvalPrompt) -> Iterable[str]:
    for m in p.messages:
        yield m.content
    for text in (p.reference_answer, p.notes):
        if text:
            yield text
    yield from p.key_points or []
    if p.check:
        yield json.dumps(p.check.spec, ensure_ascii=False)


def sensitive_findings(p: EvalPrompt) -> list[str]:
    names: set[str] = set()
    for text in prompt_texts(p):
        for line in text.splitlines() or [text]:
            names.update(scan_line(line))
            names.update(n for n, rx in EXTRA_PATTERNS.items() if rx.search(line))
    return sorted(names)


def _format_validation_error(e: ValidationError) -> str:
    parts = []
    for err in e.errors(include_url=False):
        loc = ".".join(str(x) for x in err["loc"]) or "(prompt)"
        parts.append(f"{loc}: {err['msg'].removeprefix('Value error, ')}")
    return "; ".join(parts)


def load_dataset(
    directory: Path | str = PROMPTS_DIR,
    *,
    context_window: int | None = None,
    expect_counts: bool = True,
) -> Dataset:
    """Lê e valida `*.jsonl` do diretório. Levanta `DatasetError` com todos os problemas."""
    directory = Path(directory)
    window = context_window if context_window is not None else default_context_window()
    problems: list[str] = []
    dataset = Dataset(files=sorted(directory.glob("*.jsonl")))
    if not dataset.files:
        raise DatasetError([f"nenhum arquivo .jsonl em {directory}"])
    seen: dict[str, str] = {}
    for path in dataset.files:
        for n, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if not raw.strip():
                continue
            where = f"{path.name}:{n}"
            try:
                data = json.loads(raw)
            except json.JSONDecodeError as e:
                problems.append(f"{where}: JSON inválido ({e.msg})")
                continue
            try:
                prompt = EvalPrompt.model_validate(data)
            except ValidationError as e:
                ident = data.get("id") if isinstance(data, dict) else None
                problems.append(f"{where} [{ident or '?'}]: {_format_validation_error(e)}")
                continue
            if prompt.id in seen:
                problems.append(f"{where} [{prompt.id}]: id duplicado (já definido em {seen[prompt.id]})")
                continue
            seen[prompt.id] = where
            tokens = estimate_tokens(prompt.request_body())
            if tokens > MAX_PROMPT_TOKENS:
                problems.append(
                    f"{where} [{prompt.id}]: entrada estimada em {tokens} tokens (máximo {MAX_PROMPT_TOKENS})"
                )
            if tokens + prompt.params.max_tokens > window:
                problems.append(
                    f"{where} [{prompt.id}]: entrada ({tokens}) + max_tokens ({prompt.params.max_tokens}) "
                    f"passa do contexto de {window} tokens"
                )
            sensitive = sensitive_findings(prompt)
            if sensitive:
                problems.append(f"{where} [{prompt.id}]: padrão sensível ({', '.join(sensitive)}; valor omitido)")
            dataset.prompts.append(prompt)
    if expect_counts:
        counts = Counter(p.task_type for p in dataset.prompts)
        for cat in TASK_TYPES:
            if counts[cat] != PER_CATEGORY:
                problems.append(f"categoria {cat}: {counts[cat]} prompts (esperado {PER_CATEGORY})")
    if problems:
        raise DatasetError(problems)
    return dataset


def dataset_hash(directory: Path | str = PROMPTS_DIR) -> str:
    """SHA-256 do conteúdo dos .jsonl (nomes e bytes, com fim de linha normalizado para LF)."""
    h = hashlib.sha256()
    for path in sorted(Path(directory).glob("*.jsonl")):
        h.update(path.name.encode("utf-8") + b"\0")
        h.update(path.read_bytes().replace(b"\r\n", b"\n"))
    return h.hexdigest()


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Valida o dataset de avaliação (eval/prompts/*.jsonl).")
    ap.add_argument("--dir", type=Path, default=PROMPTS_DIR)
    args = ap.parse_args(argv)
    try:
        ds = load_dataset(args.dir)
    except DatasetError as e:
        print(f"DATASET INVÁLIDO ({len(e.problems)} problema(s)):", file=sys.stderr)
        for p in e.problems:
            print(f"  {p}", file=sys.stderr)
        return 1
    counts = Counter(p.task_type for p in ds.prompts)
    summary = ", ".join(f"{c}={counts[c]}" for c in TASK_TYPES)
    digest = dataset_hash(args.dir)[:12]
    print(f"OK: {len(ds.prompts)} prompts em {len(ds.files)} arquivo(s) ({summary}); hash {digest}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
