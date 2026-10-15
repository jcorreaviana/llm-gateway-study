"""Testes do preparo do cache do tiktoken (scripts/_tiktoken_cache.py), sem litellm e sem rede."""

from __future__ import annotations

import hashlib
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
from _tiktoken_cache import (  # noqa: E402
    CL100K_URL,
    TiktokenCacheError,
    cache_key,
    expected_hashes,
    prepare_cache,
)

LF = b"IQ== 0\nIg== 1\nIw== 2\n"
CRLF = LF.replace(b"\n", b"\r\n")
LF_HASH = hashlib.sha256(LF).hexdigest()
KEY = cache_key(CL100K_URL)


def test_cache_key_is_sha1_of_the_url_like_tiktoken():
    assert KEY == "9b5ad71b2ce5302211f9c61530b329a4922fc6a4"


def test_crlf_is_converted_and_hash_checked(tmp_path):
    bundled, dest = tmp_path / "bundled", tmp_path / "dest"
    bundled.mkdir()
    (bundled / KEY).write_bytes(CRLF)
    (bundled / "anthropic_tokenizer.json").write_bytes(b"{}")  # nao e do tiktoken: ignorado
    prepared = prepare_cache(bundled, dest, {KEY: LF_HASH}, required={KEY})
    assert prepared == [KEY]
    assert (dest / KEY).read_bytes() == LF
    assert [p.name for p in dest.iterdir()] == [KEY]
    assert (bundled / KEY).read_bytes() == CRLF  # a origem nunca e alterada


def test_hash_mismatch_aborts_and_writes_nothing(tmp_path):
    bundled, dest = tmp_path / "bundled", tmp_path / "dest"
    bundled.mkdir()
    (bundled / KEY).write_bytes(CRLF)
    with pytest.raises(TiktokenCacheError, match="nao bate"):
        prepare_cache(bundled, dest, {KEY: "0" * 64}, required={KEY})
    assert not (dest / KEY).exists()


def test_missing_required_file_aborts(tmp_path):
    bundled = tmp_path / "bundled"
    bundled.mkdir()
    with pytest.raises(TiktokenCacheError, match="obrigatorios"):
        prepare_cache(bundled, tmp_path / "dest", {KEY: LF_HASH}, required={KEY})


def test_other_expected_files_are_prepared_when_present(tmp_path):
    other = cache_key("https://exemplo.invalid/outro.tiktoken")
    bundled, dest = tmp_path / "bundled", tmp_path / "dest"
    bundled.mkdir()
    (bundled / KEY).write_bytes(CRLF)
    (bundled / other).write_bytes(CRLF)
    hashes = {KEY: LF_HASH, other: LF_HASH}
    assert prepare_cache(bundled, dest, hashes, required={KEY}) == sorted([KEY, other])


def test_expected_hashes_are_read_from_source_text(tmp_path):
    source = tmp_path / "openai_public.py"
    source.write_text(f'load("{CL100K_URL}",\n        expected_hash="{"ab" * 32}",\n)\n', encoding="utf-8")
    assert expected_hashes(source) == {KEY: "ab" * 32}


def test_expected_hashes_without_pairs_raises(tmp_path):
    source = tmp_path / "openai_public.py"
    source.write_text("nada aqui\n", encoding="utf-8")
    with pytest.raises(TiktokenCacheError):
        expected_hashes(source)
