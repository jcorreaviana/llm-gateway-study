"""Isolamento global: nenhum teste escreve em data/ ou logs/ do projeto, e nenhum herda
ENABLE_REAL_API/ANTHROPIC_API_KEY do ambiente da máquina."""

import pytest

import gateway.config as gateway_config


@pytest.fixture(autouse=True)
def _isolate_project_paths(tmp_path, monkeypatch):
    # Caminhos relativos do config (data/budget.json, logs/requests.jsonl) valem a partir daqui.
    monkeypatch.setattr(gateway_config, "PROJECT_ROOT", tmp_path)
    monkeypatch.delenv("ENABLE_REAL_API", raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
