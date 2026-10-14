"""Estimativa de tokens do dataset: a MESMA aproximação do gateway (caracteres/4 sobre o texto das
mensagens). Fica separada de `gateway.providers` de propósito: aquele módulo também define o cliente da
Anthropic, e o código de avaliação não deve importá-lo. Um teste confere a paridade com o gateway."""

from typing import Any


def estimate_tokens(request: dict[str, Any]) -> int:
    chars = sum(len(m["content"]) for m in request.get("messages", []) if isinstance(m.get("content"), str))
    return chars // 4 + 1
