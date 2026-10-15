"""Embedding opcional: dependência ausente, modelo fora do cache e o modelo real (só integração).
Nenhum teste padrão baixa nada nem fala com a rede."""

import sys
import types

import pytest

from evalkit import embedding
from evalkit.embedding import DEFAULT_MODEL, EmbeddingUnavailable, SentenceTransformerEmbedder, cosine, load_embedder

pytestmark = pytest.mark.usefixtures("offline_hf")  # nada aqui deve tentar a rede do Hugging Face


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
        def __init__(self, name, **kw):
            seen.update(name=name, **kw)
            raise OSError("não está no cache")

    monkeypatch.setitem(sys.modules, "sentence_transformers", fake_module(Model))
    emb, status = load_embedder()
    assert emb is None and "OSError" in status
    assert seen["local_files_only"] is True and seen["name"] == DEFAULT_MODEL  # padrão: só o cache local


def test_download_only_when_explicitly_allowed(monkeypatch):
    seen = []

    class Model:
        def __init__(self, name, **kw):
            seen.append(kw["local_files_only"])

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
    assert emb.info()["device"] == "cpu" and emb.info()["revision"] == embedding.DEFAULT_REVISION
    a, b, c = emb.embed([
        "A nova ciclovia de 12 quilômetros liga o centro ao bairro da Serra.",
        "Uma ciclovia de 12 km conecta o centro ao bairro Serra.",
        "O campeonato de xadrez começa na próxima sexta-feira.",
    ])
    assert cosine(a, b) > cosine(a, c) + 0.2


class RecordingModel:
    """SentenceTransformer falso que guarda como foi construído e simula o tokenizador."""

    instances: list = []
    max_seq_length = 128
    device = "cpu"

    def __init__(self, name, **kw):
        self.kw = {"name": name, **kw}
        RecordingModel.instances.append(self)

    def tokenizer(self, texts, add_special_tokens, truncation):
        assert truncation is False  # mede o texto inteiro, sem cortar
        return {"input_ids": [[0] * len(t.split()) for t in texts]}

    def encode(self, texts, convert_to_numpy):
        return [[1.0, 0.0] for _ in texts]


def test_default_model_is_pinned_to_a_revision_and_forced_to_cpu(monkeypatch):
    RecordingModel.instances.clear()
    monkeypatch.setitem(sys.modules, "sentence_transformers", fake_module(RecordingModel))
    emb, _ = load_embedder()
    kw = RecordingModel.instances[0].kw
    assert kw["revision"] == embedding.DEFAULT_REVISION and len(embedding.DEFAULT_REVISION) == 40
    assert kw["device"] == "cpu" and kw["local_files_only"] is True
    assert emb.info() == {"model": DEFAULT_MODEL, "revision": embedding.DEFAULT_REVISION, "device": "cpu",
                          "max_seq_length": 128}


def test_other_models_are_not_silently_given_the_default_revision(monkeypatch):
    RecordingModel.instances.clear()
    monkeypatch.setitem(sys.modules, "sentence_transformers", fake_module(RecordingModel))
    emb, _ = load_embedder("outro/modelo")
    assert RecordingModel.instances[0].kw["revision"] is None and emb.info()["revision"] == "unpinned"
    emb2, _ = load_embedder("outro/modelo", revision="abc")
    assert emb2.info()["revision"] == "abc"


def test_truncation_is_detected_past_max_seq_length(monkeypatch):
    monkeypatch.setitem(sys.modules, "sentence_transformers", fake_module(RecordingModel))
    emb, _ = load_embedder()
    assert emb.truncated(["curto", "palavra " * 200]) == [False, True]
    assert emb.truncated(["x " * 128]) == [False]  # no limite exato não corta


def test_summarization_details_record_truncation(monkeypatch):
    from evalkit.scorers import score_record  # noqa: PLC0415
    from tests.test_scorers import GOOD_SUMMARY, rec, summary_prompt  # noqa: PLC0415

    monkeypatch.setitem(sys.modules, "sentence_transformers", fake_module(RecordingModel))
    emb, _ = load_embedder()
    short = score_record(summary_prompt(), rec(GOOD_SUMMARY), emb)["details"]
    assert short["embedding_truncated"] == {"reference": False, "answer": False, "max_seq_length": 128}
    long = score_record(summary_prompt(), rec("palavra " * 300), emb)["details"]
    assert long["embedding_truncated"]["answer"] is True and long["embedding_truncated"]["reference"] is False
