"""Integração do comparativo (desligada por padrão: `pytest -m integration`).

Roda um conjunto MÍNIMO de cenas (normal e 429 com Retry-After curto) nos dois gateways, com processos
reais: o gateway do projeto, o proxy do LiteLLM (com a trava de rede, o sumidouro e o monitor de conexões),
o mock e o Ollama. `skip` com mensagem clara se o litellm ou o Ollama/modelos não estiverem disponíveis.
Nenhuma chamada à Anthropic. Os resultados vão para um diretório temporário.
"""

import json

import pytest

from comparekit import report
from scripts import compare_gateways, litellm_smoke

pytestmark = pytest.mark.integration


def test_minimal_scenes_in_both_gateways(tmp_path, monkeypatch):
    if (reason := litellm_smoke.proxy_available()) is not None:
        pytest.skip(f"litellm nao instalado: {reason} (veja requirements-litellm.lock.txt)")
    if (reason := litellm_smoke.ollama_ready()) is not None:
        pytest.skip(reason)
    monkeypatch.setattr(compare_gateways, "RESULTS_ROOT", tmp_path)

    code = compare_gateways.run_all("itest", ("proprio", "litellm"), {"1", "3a"}, reps=1)

    assert code == 0  # sem bloqueio da trava, sem conexão externa, sem processo restante
    data = json.loads((tmp_path / "itest" / "results.json").read_text(encoding="utf-8"))
    runs = {(r["scene"], r["gateway"]): r for r in data["runs"]}
    assert set(runs) == {("1", "proprio"), ("1", "litellm"), ("3a", "proprio"), ("3a", "litellm")}
    for gateway in ("proprio", "litellm"):
        normal = runs[("1", gateway)]
        assert [q["status"] for q in normal["requests"]] == [200, 200, 200]
        assert {q["provider"] for q in normal["requests"]} == {"mock_market"}
        assert normal["mock"]["received"] == 3
        limited = runs[("3a", gateway)]
        assert limited["requests"][0]["provider"] == "ollama_qwen"       # o 429 do mock cai para o Qwen
        assert limited["mock"]["by_status"].get("429") == 1
    assert data["meta"]["litellm_version"]
    guard = json.loads((tmp_path / "itest" / "netguard.json").read_text(encoding="utf-8"))
    assert guard["blocked"] == [] and guard["external_connections"] == [] and guard["orphans"] == []
    # o mesmo resultado gera tabelas e CSV sem texto de resposta
    csv_text = report.render_csv(data["runs"])
    assert csv_text.splitlines()[0] == ",".join(report.CSV_FIELDS)
    assert "[mock]" not in csv_text
