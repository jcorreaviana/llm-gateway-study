"""Isolamento global: nenhum teste escreve em data/ ou logs/ do projeto, e nenhum herda
ENABLE_REAL_API/ANTHROPIC_API_KEY do ambiente da máquina."""

import sys
from pathlib import Path

import pytest

import gateway.config as gateway_config
from tests.netguard import NetworkGuard

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


@pytest.fixture
def offline_hf(monkeypatch):
    """Modo offline do Hugging Face para os testes que carregam (ou simulam o carregamento do) modelo de embedding.
    As variáveis sozinhas não bastam se `huggingface_hub` já foi importado (as constantes são lidas no import), então
    a constante também é ajustada nesse caso."""
    monkeypatch.setenv("HF_HUB_OFFLINE", "1")
    monkeypatch.setenv("TRANSFORMERS_OFFLINE", "1")
    constants = sys.modules.get("huggingface_hub.constants")
    if constants is not None:
        monkeypatch.setattr(constants, "HF_HUB_OFFLINE", True, raising=False)


@pytest.fixture(autouse=True)
def _block_external_network():
    """Só loopback: um teste que tente falar com a internet (ou com a Anthropic) falha, mesmo que
    o código sob teste engula a exceção."""
    guard = NetworkGuard()
    guard.install()
    try:
        yield guard
    finally:
        guard.uninstall()
    assert not guard.violations, f"tentativa de conexão externa: {guard.violations}"


@pytest.fixture(scope="session", autouse=True)
def _repo_state_dirs_untouched():
    """Falha a sessão se algum teste criar, alterar ou apagar arquivos em data/ ou logs/ do repo."""
    before = _snapshot()
    yield
    after = _snapshot()
    assert after == before, f"a suíte alterou data/ ou logs/ do repositório: {set(after) ^ set(before) or 'conteúdo/mtime'}"
