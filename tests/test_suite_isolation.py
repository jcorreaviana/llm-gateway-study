"""A própria isolação da suíte: tmp_path no lugar de data/ e logs/, e ambiente limpo."""

import os

import gateway.config as gateway_config
from gateway.config import AppConfig, resolve_path
from tests.gw_helpers import make_config


def test_project_root_is_redirected_to_tmp(tmp_path):
    assert gateway_config.PROJECT_ROOT == tmp_path
    cfg: AppConfig = make_config()
    assert resolve_path(cfg.budget.path) == tmp_path / "data" / "budget.json"
    assert resolve_path(cfg.recorder.path) == tmp_path / "logs" / "requests.jsonl"


def test_secret_env_vars_are_cleared():
    assert "ENABLE_REAL_API" not in os.environ
    assert "ANTHROPIC_API_KEY" not in os.environ


def test_env_changes_do_not_leak_between_tests_a(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "x" * 20)
    monkeypatch.setenv("ENABLE_REAL_API", "1")


def test_env_changes_do_not_leak_between_tests_b():
    # em qualquer ordem (pytest-randomly), o teste "a" não deixa rastro aqui
    assert "ANTHROPIC_API_KEY" not in os.environ and "ENABLE_REAL_API" not in os.environ
