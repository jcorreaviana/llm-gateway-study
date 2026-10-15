"""Cache do tiktoken em diretório temporário para o smoke do LiteLLM Proxy (sem rede).

Por quê: o wheel do LiteLLM 1.103.1 embute os arquivos de ranks do tiktoken (`cl100k_base` etc.) com fim de
linha CRLF. O tiktoken confere o SHA-256 do arquivo de cache, rejeita o CRLF, apaga o arquivo e tenta baixar de
`openaipublic.blob.core.windows.net` na inicialização. O conteúdo com LF bate com o hash oficial.

Contorno: copiar para um diretório temporário (fora do repositório e do venv) convertendo CRLF -> LF e conferindo
o SHA-256 contra o hash que o PRÓPRIO tiktoken instalado espera (lido de `tiktoken_ext/openai_public.py`, sem
importá-lo). O nome do arquivo no cache é o SHA-1 da URL, como em `tiktoken.load.read_file_cached`. Se qualquer
hash não bater, aborta: quem decide é o usuário. Nada dentro do venv é alterado.
"""

from __future__ import annotations

import hashlib
import re
from pathlib import Path

CL100K_URL = "https://openaipublic.blob.core.windows.net/encodings/cl100k_base.tiktoken"
_URL_HASH = re.compile(r'"(https://[^"]+)",\s*expected_hash="([0-9a-f]{64})"')


class TiktokenCacheError(Exception):
    """O cache do tiktoken não pôde ser preparado de forma verificável."""


def cache_key(url: str) -> str:
    """Nome do arquivo no cache do tiktoken (SHA-1 da URL), igual a `read_file_cached`."""
    return hashlib.sha1(url.encode()).hexdigest()


def expected_hashes(openai_public: Path) -> dict[str, str]:
    """{nome do arquivo de cache: SHA-256 esperado}, lido do texto de `tiktoken_ext/openai_public.py`."""
    found = {cache_key(url): digest for url, digest in _URL_HASH.findall(openai_public.read_text(encoding="utf-8"))}
    if not found:
        raise TiktokenCacheError(f"nenhum par URL/hash encontrado em {openai_public.name}")
    return found


def prepare_cache(bundled: Path, dest: Path, expected: dict[str, str], required: set[str]) -> list[str]:
    """Copia para `dest` só os arquivos embutidos que o tiktoken conhece, com CRLF -> LF, cada um conferido por
    SHA-256. Devolve os nomes preparados. Aborta se faltar um `required` ou se qualquer hash não bater."""
    prepared: list[str] = []
    dest.mkdir(parents=True, exist_ok=True)
    for name in sorted(expected):
        source = bundled / name
        if not source.is_file():
            continue
        data = source.read_bytes().replace(b"\r\n", b"\n")
        digest = hashlib.sha256(data).hexdigest()
        if digest != expected[name]:
            raise TiktokenCacheError(
                f"hash do cache {name} nao bate depois de converter CRLF para LF "
                f"(obtido {digest[:12]}..., esperado {expected[name][:12]}...)")
        (dest / name).write_bytes(data)
        prepared.append(name)
    missing = sorted(required - set(prepared))
    if missing:
        raise TiktokenCacheError(f"arquivos obrigatorios ausentes no cache embutido: {missing}")
    return prepared
