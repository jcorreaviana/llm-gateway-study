"""Recorder: uma linha JSON por requisição em `logs/requests.jsonl` (insumo da Fase 3).

Guarda prompt e resposta, então o arquivo é local e ignorado pelo git. Padrões de segredo são
mascarados no conteúdo gravado. Com `record_content: false` só vão metadados. Uma falha de
gravação NUNCA derruba a requisição: vira um aviso no log, sem conteúdo.
"""

import json
import logging
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from gateway.secrets import mask_deep

log = logging.getLogger("gateway.recorder")


class Recorder:
    def __init__(self, path: Path | str, record_content: bool = True) -> None:
        self.path = Path(path)
        self.record_content = record_content
        self._lock = threading.Lock()

    def build(
        self,
        *,
        request_id: str,
        request: dict[str, Any] | None,
        provider: str,
        forced_provider: bool,
        fallback_reason: str,
        status: int,
        latency_ms: float,
        response: dict[str, Any] | None,
    ) -> dict[str, Any]:
        request = request or {}
        usage = response.get("usage") if isinstance(response, dict) else None
        entry: dict[str, Any] = {
            "request_id": request_id,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "temperature": request.get("temperature"),
            "max_tokens": request.get("max_tokens"),
            "provider": provider,
            "forced_provider": forced_provider,
            "fallback_reason": fallback_reason or None,
            "status": status,
            "latency_ms": round(latency_ms, 1),
            "usage": usage if isinstance(usage, dict) else None,
        }
        if self.record_content:
            entry["messages"] = mask_deep(request.get("messages"))
            entry["response"] = mask_deep(_response_text(response))
        return entry

    def write(self, entry: dict[str, Any]) -> None:
        """Acrescenta uma linha, com lock. Nunca levanta exceção."""
        try:
            line = json.dumps(entry, ensure_ascii=False)
            with self._lock:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                with open(self.path, "a", encoding="utf-8", newline="\n") as f:
                    f.write(line + "\n")
        except Exception as e:  # noqa: BLE001 - gravar é auxiliar; nunca derruba a requisição
            log.warning("recorder: falha ao gravar (%s)", type(e).__name__)


def _response_text(response: dict[str, Any] | None) -> str | None:
    try:
        return response["choices"][0]["message"]["content"]  # type: ignore[index]
    except (KeyError, IndexError, TypeError):
        return None
