"""Embedding opcional: dependência ausente, modelo fora do cache e o modelo real (só integração).
Nenhum teste padrão baixa nada nem fala com a rede."""

import sys
import types

import pytest

from evalkit import embedding
from evalkit.embedding import DEFAULT_MODEL, EmbeddingUnavailable, SentenceTransformerEmbedder, cosine, load_embedder


def fake_module(model_cls):
    mod = types.ModuleType("sentence_transformers")
    mod.SentenceTransformer = model_cls
    return mod


def test_missing_dependency_is_unavailable_not_an_error(monkeypatch):
    monkeypatch.setitem(sys.modules, "sentence_transformers", None)  # import falha
    emb, status = load_embedder()
    assert emb is None and status.startswith("unavailable") and "requirements-eval.txt" in status
    with pytest.raises(EmbeddingUnavailable):
        SentenceTransformerEmbedder()


def test_model_not_in_cache_is_unavailable_and_download_is_never_implicit(monkeypatch):
    seen = {}

    class Model:
        def __init__(self, name, local_files_only):
            seen.update(name=name, local_files_only=local_files_only)
            raise OSError("não está no cache")

    monkeypatch.setitem(sys.modules, "sentence_transformers", fake_module(Model))
    emb, status = load_embedder()
    assert emb is None and "OSError" in status
    assert seen == {"name": DEFAULT_MODEL, "local_files_only": True}  # padrão: só o cache local


def test_download_only_when_explicitly_allowed(monkeypatch):
    seen = []

    class Model:
        def __init__(self, name, local_files_only):
            seen.append(local_files_only)

        def encode(self, texts, convert_to_numpy):
            return [[1.0, 0.0] for _ in texts]

    monkeypatch.setitem(sys.modules, "sentence_transformers", fake_module(Model))
    emb, status = load_embedder(allow_download=True)
    assert status == "ok" and seen == [False]
    assert emb.embed(["a", "b"]) == [[1.0, 0.0], [1.0, 0.0]]
    assert cosine(*emb.embed(["a", "b"])) == pytest.approx(1.0)


def test_default_model_is_multilingual_minilm():
    assert embedding.DEFAULT_MODEL == "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2"


@pytest.mark.integration
def test_real_model_similarity_is_higher_for_paraphrases():
    """Só roda com `-m integration`, com a dependência instalada E o modelo já no cache local."""
    emb, status = load_embedder()
    if emb is None:
        pytest.skip(f"embedding indisponível: {status}")
    a, b, c = emb.embed([
        "A nova ciclovia de 12 quilômetros liga o centro ao bairro da Serra.",
        "Uma ciclovia de 12 km conecta o centro ao bairro Serra.",
        "O campeonato de xadrez começa na próxima sexta-feira.",
    ])
    assert cosine(a, b) > cosine(a, c) + 0.2
