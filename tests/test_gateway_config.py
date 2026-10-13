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
