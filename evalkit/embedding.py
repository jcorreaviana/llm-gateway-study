"""Similaridade por embedding (opcional). A dependência `sentence-transformers` NÃO está no
`requirements.txt` nem no CI: vive em `requirements-eval.txt` (extras `eval`).

O modelo nunca é baixado sem pedido explícito: por padrão carrega só do cache local
(`local_files_only=True`). Sem a dependência ou sem o modelo em cache, `load_embedder` devolve None e a
summarization usa só a cobertura de pontos-chave (`embedding: unavailable`). Baixar exige
`allow_download=True` (no CLI: `--allow-download`).
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from typing import Protocol

DEFAULT_MODEL = "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2"


class EmbeddingUnavailable(Exception):
    """Dependência ou modelo ausente."""


class Embedder(Protocol):
    def embed(self, texts: Sequence[str]) -> list[list[float]]: ...


def cosine(a: Sequence[float], b: Sequence[float]) -> float:
    if len(a) != len(b):
        raise ValueError("vetores de tamanhos diferentes")
    na, nb = math.sqrt(sum(x * x for x in a)), math.sqrt(sum(y * y for y in b))
    if na == 0 or nb == 0:
        return 0.0
    return sum(x * y for x, y in zip(a, b, strict=True)) / (na * nb)


class SentenceTransformerEmbedder:
    """Implementação real. O import e o carregamento são preguiçosos (só em `__init__`)."""

    def __init__(self, model_name: str = DEFAULT_MODEL, *, allow_download: bool = False) -> None:
        try:
            from sentence_transformers import SentenceTransformer
        except ImportError as e:
            raise EmbeddingUnavailable(
                "sentence-transformers não instalado (pip install -r requirements-eval.txt)"
            ) from e
        try:
            self._model = SentenceTransformer(model_name, local_files_only=not allow_download)
        except Exception as e:  # noqa: BLE001 - erros do huggingface/torch variam por versão
            raise EmbeddingUnavailable(f"modelo {model_name} indisponível ({type(e).__name__})") from e
        self.model_name = model_name

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        return [list(map(float, v)) for v in self._model.encode(list(texts), convert_to_numpy=True)]


def load_embedder(model_name: str = DEFAULT_MODEL, *, allow_download: bool = False) -> tuple[Embedder | None, str]:
    """(embedder, status). Nunca levanta: sem dependência/modelo devolve (None, motivo)."""
    try:
        return SentenceTransformerEmbedder(model_name, allow_download=allow_download), "ok"
    except EmbeddingUnavailable as e:
        return None, f"unavailable: {e}"
