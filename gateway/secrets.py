"""Mascaramento de segredos para qualquer texto que possa ser logado ou gravado."""

import os
import re
from typing import Any

# Montado por partes para o código-fonte não conter nada com "cara de chave".
_KEY_RX = re.compile("sk-" + r"[A-Za-z0-9_\-]{8,}")
MASK = "[REDACTED]"


def mask_secrets(text: str) -> str:
    """Troca chaves no formato sk-... e o valor atual de ANTHROPIC_API_KEY (se houver)."""
    key = os.environ.get("ANTHROPIC_API_KEY")
    if key and len(key) >= 8:
        text = text.replace(key, MASK)
    return _KEY_RX.sub(MASK, text)


def mask_deep(value: Any) -> Any:
    """`mask_secrets` aplicado a todas as strings de uma estrutura JSON."""
    if isinstance(value, str):
        return mask_secrets(value)
    if isinstance(value, list):
        return [mask_deep(v) for v in value]
    if isinstance(value, dict):
        return {k: mask_deep(v) for k, v in value.items()}
    return value
