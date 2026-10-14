"""Ramos de decisão que a cobertura mostrou sem teste (Marco 6): Retry-After como data HTTP,
timeout do lock do budget (fail-closed), lifespan real do app, conteúdo em partes e falha 4xx do mock."""

import json
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime

import httpx
import pytest

import gateway.budget as budget_mod
from gateway.budget import Budget
from gateway.main import create_app
from gateway.providers import estimate_tokens, parse_retry_after, to_anthropic_request
from mock_upstream.server import create_app as create_mock_app
from tests.gw_helpers import make_config

# --------------------------------------------------------------------------- Retry-After


@pytest.mark.parametrize(
    "value,expected",
    [("7", 7.0), (" 2.5 ", 2.5), ("0", 0.0), ("-3", 0.0), (None, None), ("", None), ("amanha", None)],
)
def test_parse_retry_after_seconds_and_garbage(value, expected):
    assert parse_retry_after(value) == expected


def test_parse_retry_after_http_date_future_and_past():
    future = datetime.now(timezone.utc) + timedelta(seconds=120)
    got = parse_retry_after(format_datetime(future, usegmt=True))
    assert got is not None and 100 <= got <= 121  # folga grande: o relógio real avança entre as linhas
    past = datetime.now(timezone.utc) - timedelta(hours=1)
    assert parse_retry_after(format_datetime(past, usegmt=True)) == 0.0


def test_parse_retry_after_date_without_timezone_is_treated_as_utc():
    naive = (datetime.now(timezone.utc) + timedelta(seconds=300)).strftime("%a, %d %b %Y %H:%M:%S -0000")
    got = parse_retry_after(naive)
    assert got is not None and 250 <= got <= 301


# --------------------------------------------------------------------------- budget: lock ocupado


def test_budget_thread_lock_timeout_is_fail_closed(tmp_path, monkeypatch):
    monkeypatch.setattr(budget_mod, "_LOCK_TIMEOUT_S", 0.05)
    b = Budget(tmp_path / "b.json", limit=5)
    b.ensure()
    with b._tlock:  # outra tarefa segurando o lock em memória
        assert b.reserve() is False
        status = b.status()
    assert status.remaining == 0 and "ocupado" in status.error
    assert b.reserve() is True  # ocupado não é corrupção: depois que soltou, volta a funcionar
    assert json.loads(b.path.read_text(encoding="utf-8")) == {"used": 1}


def test_budget_file_lock_held_by_another_handle_is_fail_closed(tmp_path, monkeypatch):
    monkeypatch.setattr(budget_mod, "_LOCK_TIMEOUT_S", 0.1)
    path = tmp_path / "b.json"
    holder, contender = Budget(path, limit=5), Budget(path, limit=5)  # locks de thread distintos
    holder.ensure()
    with holder.locked():  # "outro processo" com o lock de arquivo
        assert contender.reserve() is False
    assert json.loads(path.read_text(encoding="utf-8")) == {"used": 0}  # nada foi gasto
    assert contender.reserve() is True


# --------------------------------------------------------------------------- lifespan real


async def test_real_lifespan_creates_budget_at_zero_and_closes_owned_client(tmp_path):
    cfg = make_config(budget={"limit": 30, "path": str(tmp_path / "data" / "budget.json")})
    app = create_app(cfg)  # sem client injetado: o app cria e é dono do AsyncClient
    assert not (tmp_path / "data" / "budget.json").exists()
    async with app.router.lifespan_context(app):
        assert json.loads((tmp_path / "data" / "budget.json").read_text(encoding="utf-8")) == {"used": 0}
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://gw") as c:
            assert (await c.get("/health")).json() == {"status": "ok"}


async def test_lifespan_does_not_touch_existing_budget_file(tmp_path):
    path = tmp_path / "data" / "budget.json"
    path.parent.mkdir()
    path.write_text('{"used": 7}', encoding="utf-8")
    app = create_app(make_config(budget={"limit": 30, "path": str(path)}))
    async with app.router.lifespan_context(app):
        pass
    assert json.loads(path.read_text(encoding="utf-8")) == {"used": 7}


# --------------------------------------------------------------------------- conteúdo em partes


def test_estimate_tokens_counts_text_parts_and_ignores_non_text():
    parts = [{"type": "text", "text": "a" * 40}, {"type": "image_url", "image_url": {}}, "lixo"]
    assert estimate_tokens({"messages": [{"role": "user", "content": parts}]}) == 40 // 4 + 1
    assert estimate_tokens({"messages": [{"role": "user", "content": None}]}) == 1


def test_anthropic_translation_joins_text_parts_and_maps_system():
    request = {"messages": [
        {"role": "system", "content": [{"type": "text", "text": "seja breve"}]},
        {"role": "user", "content": [{"type": "text", "text": "ola "}, {"type": "text", "text": "mundo"}]},
        {"role": "tool", "content": 5},
    ]}
    body = to_anthropic_request(request, "claude-haiku-5-5", 50)
    assert body["system"] == "seja breve"
    assert body["messages"] == [
        {"role": "user", "content": "ola mundo"},
        {"role": "user", "content": ""},  # conteúdo que não é texto vira vazio, nunca quebra
    ]


# --------------------------------------------------------------------------- mock


async def test_mock_echoes_text_parts_and_ignores_other_parts():
    transport = httpx.ASGITransport(app=create_mock_app())
    async with httpx.AsyncClient(transport=transport, base_url="http://mock") as c:
        parts = [{"type": "text", "text": "ola"}, {"type": "image_url"}, {"type": "text", "text": "mundo"}]
        r = await c.post("/v1/chat/completions", json={"messages": [{"role": "user", "content": parts}]})
    assert r.json()["choices"][0]["message"]["content"] == "[mock] ola  mundo"


async def test_mock_non_5xx_non_429_failure_status_is_client_style_error():
    transport = httpx.ASGITransport(app=create_mock_app())
    async with httpx.AsyncClient(transport=transport, base_url="http://mock") as c:
        await c.post("/chaos", json={"fail_every_n": 1, "fail_status": 400})
        r = await c.post("/v1/chat/completions", json={"messages": [{"role": "user", "content": "x"}]})
    assert r.status_code == 400 and r.json()["error"]["type"] == "invalid_request_error"
    assert "Retry-After" not in r.headers
