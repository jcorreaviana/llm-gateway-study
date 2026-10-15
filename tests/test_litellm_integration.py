"""Integração do LiteLLM Proxy (desligada por padrão: `pytest -m integration`).

Sobe o proxy REAL do `.venv-litellm/` (com a trava de rede, o sumidouro e o monitor de conexões) e o mock, e
roda o smoke do Marco 13 (mock `normal` e depois `rate_limit` com fallback para o Qwen). `skip` com mensagem
clara se o litellm não estiver instalado ou se o Ollama/modelos não estiverem disponíveis. Nenhuma Anthropic.
"""

import pytest

from scripts import litellm_smoke

pytestmark = pytest.mark.integration


def test_litellm_proxy_smoke_with_fallback():
    if (reason := litellm_smoke.proxy_available()) is not None:
        pytest.skip(f"litellm nao instalado: {reason} (veja requirements-litellm.lock.txt)")
    if (reason := litellm_smoke.ollama_ready()) is not None:
        pytest.skip(reason)
    report = litellm_smoke.run_smoke()
    normal, limited = report.steps
    assert normal["status"] == 200 and normal["headers"]["x-litellm-model-id"] == "mock_market"
    assert limited["status"] == 200 and limited["headers"]["x-litellm-model-id"] == "ollama_qwen"
    assert limited["headers"]["x-litellm-attempted-fallbacks"] == "1"
    assert report.mock_stats[-1]["by_status"] == {"200": 1, "429": 1}
    assert report.netguard_blocked == [] and report.external_connections == []
    assert report.orphans == []
