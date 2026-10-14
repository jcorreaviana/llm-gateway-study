"""Isolamento global: nenhum teste escreve em data/ ou logs/ do projeto, e nenhum herda
ENABLE_REAL_API/ANTHROPIC_API_KEY do ambiente da máquina."""

from pathlib import Path

import pytest

import gateway.config as gateway_config

REPO_ROOT = Path(__file__).resolve().parent.parent
GUARDED_DIRS = ("data", "logs")


def _snapshot() -> dict[str, tuple[int, int]]:
    """(tamanho, mtime_ns) de cada arquivo em data/ e logs/ do repositório."""
    snap = {}
    for name in GUARDED_DIRS:
        for f in sorted((REPO_ROOT / name).rglob("*")):
            if f.is_file():
                st = f.stat()
                snap[str(f.relative_to(REPO_ROOT))] = (st.st_size, st.st_mtime_ns)
    return snap


@pytest.fixture(autouse=True)
def _isolate_project_paths(tmp_path, monkeypatch):
    # Caminhos relativos do config (data/budget.json, logs/requests.jsonl) valem a partir daqui.
    monkeypatch.setattr(gateway_config, "PROJECT_ROOT", tmp_path)
    monkeypatch.delenv("ENABLE_REAL_API", raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)


@pytest.fixture(scope="session", autouse=True)
def _repo_state_dirs_untouched():
    """Falha a sessão se algum teste criar, alterar ou apagar arquivos em data/ ou logs/ do repo."""
    before = _snapshot()
    yield
    after = _snapshot()
    assert after == before, f"a suíte alterou data/ ou logs/ do repositório: {set(after) ^ set(before) or 'conteúdo/mtime'}"
