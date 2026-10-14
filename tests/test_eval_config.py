"""config.eval.yaml: mesmo schema dos outros configs, medir sem proteger e nunca a Anthropic."""

from pathlib import Path

from gateway.config import load_config

EVAL_CONFIG = Path(__file__).resolve().parent.parent / "config.eval.yaml"


def test_eval_config_loads_with_the_shared_schema():
    cfg = load_config(EVAL_CONFIG)
    assert cfg.gateway.force_provider_enabled is True
    assert cfg.recorder.record_content is True


def test_eval_chain_is_only_the_two_local_models():
    cfg = load_config(EVAL_CONFIG)
    assert cfg.chain == ["ollama_qwen", "ollama_llama"]
    assert {p.name for p in cfg.providers} == {"anthropic", "ollama_qwen", "ollama_llama"}  # sem mock_market


def test_eval_config_never_enables_anthropic():
    cfg = load_config(EVAL_CONFIG)
    assert cfg.provider("anthropic").enabled is False
    assert "anthropic" not in cfg.chain


def test_eval_config_measures_instead_of_protecting():
    cfg = load_config(EVAL_CONFIG)
    for name in ("ollama_qwen", "ollama_llama"):
        p = cfg.provider(name)
        assert p.rate_limit is None  # sem rate limit local
        assert p.breaker.failure_threshold >= 1000
        assert p.timeout_s >= 120
        assert p.warmup is True
        assert p.context_window == 3072  # respeita o contexto carregado (4096) com margem
