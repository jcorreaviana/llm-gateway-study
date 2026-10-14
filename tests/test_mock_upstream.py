import time

import httpx
import pytest

from mock_upstream.server import create_app

BODY = {"messages": [{"role": "user", "content": "ola mundo"}]}


@pytest.fixture
async def client():
    transport = httpx.ASGITransport(app=create_app())
    async with httpx.AsyncClient(transport=transport, base_url="http://mock") as c:
        yield c


async def chat(client):
    return await client.post("/v1/chat/completions", json=BODY)


async def test_normal_response_openai_format(client):
    r = await chat(client)
    assert r.status_code == 200
    data = r.json()
    assert data["id"]
    assert data["model"]
    assert data["choices"][0]["message"]["content"] == "[mock] ola mundo"
    usage = data["usage"]
    assert usage["total_tokens"] == usage["prompt_tokens"] + usage["completion_tokens"]
    assert usage["prompt_tokens"] > 0


async def test_rate_limit_returns_429_with_retry_after(client):
    await client.post("/chaos", json={"mode": "rate_limit", "retry_after_seconds": 7})
    r = await chat(client)
    assert r.status_code == 429
    assert r.headers["Retry-After"] == "7"
    assert r.json()["error"]["type"] == "rate_limit_error"


@pytest.mark.parametrize("status", [500, 503])
async def test_server_error_uses_configured_status(client, status):
    await client.post("/chaos", json={"mode": "server_error", "error_status": status})
    r = await chat(client)
    assert r.status_code == status
    assert "message" in r.json()["error"]


async def test_slow_respects_latency():
    """Mede o PEDIDO de espera (sleep injetado), não o relógio: nada de limite em ms."""
    waits: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        waits.append(seconds)

    transport = httpx.ASGITransport(app=create_app(sleep=fake_sleep))
    async with httpx.AsyncClient(transport=transport, base_url="http://mock") as c:
        assert (await chat(c)).status_code == 200
        assert waits == []  # modo normal não espera
        await c.post("/chaos", json={"mode": "slow", "latency_ms": 150})
        r = await chat(c)
    assert r.status_code == 200
    assert waits == [0.15]


async def test_slow_really_waits_with_generous_tolerance(client):
    """Sanidade com relógio real, só limite inferior e folgado (o timer do Windows tem ~15 ms
    de granularidade): esperou pelo menos metade do pedido."""
    await client.post("/chaos", json={"mode": "slow", "latency_ms": 400})
    start = time.monotonic()
    r = await chat(client)
    assert r.status_code == 200
    assert time.monotonic() - start >= 0.2


async def test_fail_every_n_fails_exactly_on_nth(client):
    await client.post("/chaos", json={"fail_every_n": 3, "fail_status": 502})
    statuses = [(await chat(client)).status_code for _ in range(6)]
    assert statuses == [200, 200, 502, 200, 200, 502]


async def test_reset_returns_to_normal(client):
    await client.post("/chaos", json={"mode": "server_error", "fail_every_n": 2})
    assert (await chat(client)).status_code == 500
    r = await client.post("/chaos/reset")
    assert r.json()["mode"] == "normal"
    assert (await client.get("/chaos")).json()["fail_every_n"] is None
    assert [(await chat(client)).status_code for _ in range(4)] == [200] * 4


@pytest.mark.parametrize(
    "payload",
    [
        {"mode": "explode"},
        {"latency_ms": -1},
        {"retry_after_seconds": -5},
        {"fail_every_n": -2},
        {"fail_every_n": 0},
        {"error_status": 418},
        {"fail_status": 200},
        {"unknown_field": 1},
    ],
)
async def test_invalid_chaos_payload_returns_422(client, payload):
    r = await client.post("/chaos", json=payload)
    assert r.status_code == 422
    assert (await client.get("/chaos")).json()["mode"] == "normal"


async def test_health(client):
    r = await client.get("/health")
    assert r.status_code == 200


async def test_stats_count_requests_by_status(client):
    for _ in range(2):
        await chat(client)
    await client.post("/chaos", json={"mode": "rate_limit"})
    await chat(client)
    stats = (await client.get("/stats")).json()
    assert stats["received"] == 3
    assert stats["by_status"] == {"200": 2, "429": 1}

    await client.post("/stats/reset")
    assert (await client.get("/stats")).json() == {"received": 0, "by_status": {}}
