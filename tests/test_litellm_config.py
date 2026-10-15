"""Testes do config do LiteLLM (litellm/config.litellm.yaml) e do isolamento da dependência.

Só parse de YAML e de texto: não precisam do litellm instalado nem de rede.
"""

from __future__ import annotations

import re
from pathlib import Path
from urllib.parse import urlsplit

import yaml

ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = ROOT / "litellm" / "config.litellm.yaml"
DEMO = yaml.safe_load((ROOT / "config.demo.yaml").read_text(encoding="utf-8"))
CFG = yaml.safe_load(CONFIG_PATH.read_text(encoding="utf-8"))
TEXT = CONFIG_PATH.read_text(encoding="utf-8")
HEADER = "\n".join(line for line in TEXT.splitlines() if line.startswith("#"))
CODE = "\n".join(line.split("#", 1)[0] for line in TEXT.splitlines())  # sem comentários
DEMO_PROVIDERS = {p["name"]: p for p in DEMO["providers"]}
GROUPS = {d["model_name"]: d for d in CFG["model_list"]}


def test_no_anthropic_provider_anywhere_in_config_values():
    assert not re.search(r"anthropic|claude", CODE, re.IGNORECASE)


def test_only_loopback_hosts():
    hosts = {urlsplit(d["litellm_params"]["api_base"]).hostname for d in CFG["model_list"]}
    assert hosts <= {"127.0.0.1", "localhost"}
    assert all(urlsplit(d["litellm_params"]["api_base"]).path == "/v1" for d in CFG["model_list"])
    assert not re.search(r"https?://(?!127\.0\.0\.1|localhost)", CODE)


def test_providers_are_openai_compatible_with_api_base():
    for d in CFG["model_list"]:
        assert d["litellm_params"]["model"].startswith("openai/")


def test_no_key_in_config():
    assert not re.search(r"sk-[A-Za-z0-9_-]{8,}|AKIA[0-9A-Z]{12,}|Bearer\s+\S+", CODE)
    assert {d["litellm_params"]["api_key"] for d in CFG["model_list"]} == {"local-placeholder"}
    assert CFG["general_settings"]["master_key"] == "os.environ/LITELLM_MASTER_KEY"  # referência, nunca o valor


def test_config_is_file_only_without_database_or_ui():
    assert "database_url" not in CODE.lower()
    assert "ui_username" not in CODE.lower()
    assert "store_model_in_db" not in CODE.lower()


def test_fallback_order_matches_demo_chain():
    chain = DEMO["chain"]
    assert CFG["router_settings"]["fallbacks"] == [{chain[0]: chain[1:]}]
    assert set(GROUPS) == set(chain)


def test_hidden_retries_are_disabled():
    assert CFG["router_settings"]["num_retries"] == 0
    assert CFG["litellm_settings"]["num_retries"] == 0


def test_limits_match_demo_values():
    mock = DEMO_PROVIDERS["mock_market"]
    router = CFG["router_settings"]
    assert GROUPS["mock_market"]["litellm_params"]["rpm"] == mock["rate_limit"]["rpm"]
    assert router["allowed_fails"] == mock["breaker"]["failure_threshold"]
    assert router["cooldown_time"] == mock["breaker"]["cooldown_s"]
    for name, group in GROUPS.items():
        assert group["litellm_params"]["timeout"] == DEMO_PROVIDERS[name]["timeout_s"]
        assert group["model_info"]["max_input_tokens"] == DEMO_PROVIDERS[name]["context_window"]
        assert group["litellm_params"]["model"].endswith(DEMO_PROVIDERS[name]["model"])


def test_equivalence_map_covers_every_demo_field():
    fields = set(DEMO["gateway"]) | set(DEMO["mock_upstream"]) | set(DEMO["budget"]) | set(DEMO["recorder"])
    fields |= {"chain"} | {k for p in DEMO["providers"] for k in p} | set(DEMO_PROVIDERS["mock_market"]["breaker"])
    fields |= set(DEMO_PROVIDERS["mock_market"]["rate_limit"])
    missing = sorted(f for f in fields if f not in HEADER)
    assert missing == []


def test_map_flags_unverified_behaviour_instead_of_assuming():
    assert HEADER.count("[VERIFICAR]") >= 10
    for topic in ("Retry-After", "JANELA", "contexto", "TPM", "forçar"):
        assert any(topic.lower() in line.lower() and "[VERIFICAR]" in HEADER for line in HEADER.splitlines()), topic


def test_telemetry_note_points_to_the_cli_flag():
    assert "--telemetry False" in HEADER
    assert "LITELLM_TELEMETRY NÃO existe" in HEADER


# ---------------------------------------------------------------- isolamento da dependência
def test_litellm_venv_is_gitignored():
    lines = {line.strip() for line in (ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()}
    assert ".venv-litellm/" in lines


def test_litellm_is_not_in_project_requirements_or_ci():
    for name in ("requirements.txt", "requirements-eval.txt"):
        assert "litellm" not in (ROOT / name).read_text(encoding="utf-8").lower()
    ci = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    assert "litellm" not in ci.lower()
    assert "requirements-litellm" not in ci


def test_input_pins_exact_version():
    lines = [x for x in (ROOT / "requirements-litellm.txt").read_text(encoding="utf-8").splitlines()
             if x and not x.startswith("#")]
    assert lines == ["litellm[proxy]==1.103.1"]


def test_lock_is_fully_hashed_and_documented():
    lock = (ROOT / "requirements-litellm.lock.txt").read_text(encoding="utf-8")
    blocks = re.split(r"\n(?=[A-Za-z0-9_.-]+==)", lock)[1:]
    assert len(blocks) == 116
    assert all("--hash=sha256:" in b for b in blocks)
    assert "litellm==1.103.1" in lock
    head = lock.split("\n\n", 1)[0]
    assert "WINDOWS" in head and "pywin32" in head and "NAO instala" in head
    assert not re.search(r"[A-Za-z]:[\/]|Users|AppData", re.sub(r"--hash=\S+", "", lock))
