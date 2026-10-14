from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from gateway.config import AppConfig, load_config


def provider(name, type_="mock", **kw):
    base = {
        "name": name,
        "type": type_,
        "base_url": "http://localhost:1",
        "model": "m",
        "context_window": 1000,
    }
    return {**base, **kw}


def cfg(**kw):
    base = {"chain": ["a"], "providers": [provider("a")]}
    return {**base, **kw}


def test_repo_config_is_valid():
    c = load_config()
    assert c.chain == ["mock_market", "ollama_qwen", "ollama_llama"]
    assert c.gateway.port == 8000
    assert c.gateway.force_provider_enabled is False
    assert c.provider("anthropic").enabled is False
    assert "anthropic" not in c.chain


def test_demo_config_is_valid_and_keeps_the_demo_limits():
    repo = Path(__file__).resolve().parent.parent
    main, demo = load_config(repo / "config.yaml"), load_config(repo / "config.demo.yaml")
    assert main.chain == demo.chain
    assert demo.provider("mock_market").rate_limit.rpm == 10
    assert demo.provider("mock_market").breaker.failure_threshold == 3
    assert demo.provider("mock_market").breaker.cooldown_s < main.provider("mock_market").breaker.cooldown_s
    assert demo.provider("ollama_qwen").warmup and demo.provider("ollama_llama").warmup
    assert demo.provider("anthropic").enabled is False and "anthropic" not in demo.chain
    assert demo.gateway.force_provider_enabled is True
    assert main.gateway.force_provider_enabled is False


def test_duplicate_provider_name_fails():
    with pytest.raises(ValidationError, match="duplicados: a"):
        AppConfig.model_validate(cfg(providers=[provider("a"), provider("a")]))


def test_unknown_name_in_chain_fails():
    with pytest.raises(ValidationError, match="inexistente: ghost"):
        AppConfig.model_validate(cfg(chain=["a", "ghost"]))


def test_repeated_name_in_chain_fails():
    with pytest.raises(ValidationError, match="repete provedor: a"):
        AppConfig.model_validate(cfg(chain=["a", "a"]))


def test_api_key_in_yaml_is_rejected():
    with pytest.raises(ValidationError, match="api_key"):
        AppConfig.model_validate(cfg(providers=[provider("a", **{"api" + "_key": "x"})]))


def test_ollama_requires_base_url():
    with pytest.raises(ValidationError, match="base_url"):
        AppConfig.model_validate(cfg(providers=[provider("a", "ollama", base_url=None)]))


def test_invalid_config_file_fails_on_load(tmp_path):
    f = tmp_path / "c.yaml"
    f.write_text(yaml.safe_dump(cfg(chain=["nope"])), encoding="utf-8")
    with pytest.raises(ValidationError, match="inexistente: nope"):
        load_config(f)
