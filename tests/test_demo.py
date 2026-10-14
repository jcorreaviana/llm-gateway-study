"""Testes do scripts/demo.py. Sem Ollama, sem processos reais e sem rede externa: o gateway e o mock
rodam em memória (ASGITransport), o Ollama e a Anthropic são httpx.MockTransport, e o relógio é falso."""

import contextlib
import json
from pathlib import Path

import httpx
import pytest

import mock_upstream.server as mock_server
from gateway.budget import Budget
from gateway.config import AppConfig
from gateway.main import create_app
from scripts import _stack, demo

MOCK_URL = "http://127.0.0.1:9001"
OLLAMA_URL = "http://localhost:11434"
ANTHROPIC_HOST = "https://api.anthropic.com"


class Clock:
    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t


def ollama_ok(request: httpx.Request) -> httpx.Response:
    return httpx.Response(200, json={
        "model": "qwen2.5:3b",
        "choices": [{"index": 0, "message": {"role": "assistant", "content": "ola local"}}],
        "usage": {"prompt_tokens": 3, "completion_tokens": 2, "total_tokens": 5},
    })


@contextlib.asynccontextmanager
async def demo_env(tmp_path: Path, *, fast: bool = False):
    raw = demo.build_runtime_config(demo.DEMO_CONFIG, gateway_port=8000, mock_port=9001, workdir=tmp_path, fast=fast)
    cfg = AppConfig.model_validate(raw)
    clock, lines, anthropic_calls, sleeps = Clock(), [], [], []
    mock_app = mock_server.create_app()

    def anthropic(request):
        anthropic_calls.append(request)
        return httpx.Response(500)

    upstream = httpx.AsyncClient(mounts={
        MOCK_URL: httpx.ASGITransport(app=mock_app),
        OLLAMA_URL: httpx.MockTransport(ollama_ok),
        ANTHROPIC_HOST: httpx.MockTransport(anthropic),
    })
    budget = Budget(tmp_path / "data" / "budget.json", 30)
    budget.ensure()
    app = create_app(cfg, client=upstream, now=clock, budget=budget)

    async def sleep(seconds: float) -> None:
        sleeps.append(seconds)
        clock.t += seconds

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://gw") as gw, \
            httpx.AsyncClient(transport=httpx.ASGITransport(app=mock_app), base_url="http://mock") as mock:
        d = demo.Demo(gw, mock, demo.DemoSettings.from_config(cfg), sleep=sleep, out=lines.append)
        d.test = type("T", (), {"lines": lines, "anthropic_calls": anthropic_calls, "sleeps": sleeps, "clock": clock})
        yield d
    await upstream.aclose()


# --------------------------------------------------------------------------- formatação
def fake_rows() -> list[demo.Row]:
    return [
        demo.Row(1, "normal", 200, "mock_market", "", 4.2, "[mock] Say hello in one word."),
        demo.Row(2, "rate_limit", 200, "ollama_qwen", "mock_market:rate_limited", 812.6,
                 "line one\nline two with a very long tail that goes past the forty character limit"),
        demo.Row(10, "unavailable", 503, "-", "anthropic:disabled", 2, "Olá, mundo 👋"),
    ]


def test_table_is_pure_ascii_aligned_and_has_all_columns():
    table = demo.format_table(fake_rows())
    assert table.isascii()
    lines = table.splitlines()
    assert len({len(line) for line in lines}) == 1  # todas as linhas com a mesma largura
    header = lines[1]
    for col in ("#", "cena", "status", "provedor", "fallback (provedor:motivo)", "latencia (ms)", "resposta"):
        assert col in header
    assert all(set(line) <= set("+-| ") for line in (lines[0], lines[2], lines[-1]))


def test_table_cells_truncate_collapse_and_default():
    rows = fake_rows()
    table = demo.format_table(rows)
    long_text = "line one line two with a very long tail that goes past the forty character limit"
    assert demo.clip(rows[1].text) == long_text[:40]  # primeiros 40, quebra de linha colapsada
    assert "| " + long_text[:40] + " |" in table and long_text[:41] not in table
    assert demo.clip("a\nb\tc") == "a b c"
    assert demo.clip("x" * 100) == "x" * 40
    assert demo.clip(rows[2].text) == "Ol?, mundo ?"  # não-ASCII vira ?
    lines = table.splitlines()
    first = next(line for line in lines if "[mock] Say hello" in line)
    assert " | - " in first  # fallback vazio vira "-"
    assert "mock_market:rate_limited" in table and "813" in table and " 4 " in first


def test_empty_table_still_has_header():
    table = demo.format_table([])
    assert "latencia (ms)" in table and table.isascii()


METRICS = """\
# TYPE gateway_requests counter
gateway_requests_total{provider="mock_market",status="200"} 4.0
gateway_requests_total{provider="ollama_qwen",status="200"} 7.0
gateway_requests_total{provider="none",status="503"} 2.0
# TYPE gateway_fallback counter
gateway_fallback_total{from_provider="mock_market",to_provider="ollama_qwen",reason="rate_limited"} 3.0
gateway_fallback_total{from_provider="mock_market",to_provider="ollama_qwen",reason="breaker_open"} 2.0
gateway_fallback_total{from_provider="anthropic",to_provider="none",reason="disabled"} 1.0
"""


def test_summary_counts_requests_per_provider_and_total_fallbacks():
    per_provider, total, by_reason = demo.parse_metrics(METRICS)
    assert per_provider == {"mock_market": 4, "ollama_qwen": 7, "none": 2}
    assert total == 6
    assert by_reason["mock_market -> ollama_qwen (rate_limited)"] == 3
    summary = demo.format_summary(METRICS)
    assert "ollama_qwen: 7" in summary and "mock_market: 4" in summary
    assert "total de requisicoes: 13" in summary and "total de fallbacks: 6" in summary
    assert summary.isascii()


def test_state_and_stats_lines():
    status = {"providers": {
        "mock_market": {"enabled": True, "breaker": {"state": "open", "consecutive_failures": 0, "open_remaining_s": 4},
                        "buckets": {"rpm": {"level": 3.46, "capacity": 10}, "tpm": None}, "cooldown_remaining_s": 2.5},
        "anthropic": {"enabled": False, "breaker": {"state": "closed", "consecutive_failures": 0},
                      "buckets": {"rpm": None, "tpm": None}, "cooldown_remaining_s": 0},
        "ollama_qwen": {"enabled": True, "breaker": {"state": "closed", "consecutive_failures": 1},
                        "buckets": {"rpm": None, "tpm": None}, "cooldown_remaining_s": 0},
    }}
    line = demo.format_state(status)
    assert "mock_market: breaker=open(0 falhas) rpm=3.5/10 cooldown=2.5s" in line
    assert "ollama_qwen: breaker=closed(1 falhas)" in line and "anthropic" not in line
    assert demo.format_stats({"received": 3, "by_status": {"500": 3}}) == "/stats do mock: recebidas=3 por_status={500:3}"


# --------------------------------------------------------------------------- ambiente e configuração
def test_child_env_disables_real_api_and_carries_no_key(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "valor-do-ambiente-pai")
    monkeypatch.setenv("ENABLE_REAL_API", "1")
    env = _stack.child_env(GATEWAY_CONFIG="x.yaml")
    assert env["ENABLE_REAL_API"] == "0"
    assert not env.get("ANTHROPIC_API_KEY")  # vazia: nunca o valor do pai
    assert "valor-do-ambiente-pai" not in env.values()
    assert env["GATEWAY_CONFIG"] == "x.yaml"
    isolated = _stack.child_env({"ANTHROPIC_API_KEY": "outra", "PATH": "p"})
    assert isolated["PATH"] == "p" and isolated["ANTHROPIC_API_KEY"] == ""


def test_start_stack_passes_the_safe_env_to_both_processes(tmp_path, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "valor-do-ambiente-pai")
    monkeypatch.setenv("ENABLE_REAL_API", "1")
    launched = []

    class FakeProc:
        returncode = 1

        def __init__(self, cmd, **kw):
            launched.append((cmd, kw))

        def poll(self):
            return 1  # já encerrou: wait_health falha logo, sem rede

    monkeypatch.setattr(_stack.subprocess, "Popen", FakeProc)
    with pytest.raises(demo.DemoError, match="encerrou antes do /health"):
        with _stack.start_stack(tmp_path / "c.yaml", tmp_path, 8123, 9123):
            pass
    assert len(launched) == 2
    for _cmd, kw in launched:
        assert kw["env"]["ENABLE_REAL_API"] == "0"
        assert not kw["env"].get("ANTHROPIC_API_KEY")
        assert kw["env"]["MOCK_UPSTREAM_PORT"] == "9123"
        assert kw["env"]["GATEWAY_CONFIG"] == str(tmp_path / "c.yaml")
    assert "--host" in launched[1][0] and launched[1][0][launched[1][0].index("--host") + 1] == "127.0.0.1"


def test_runtime_config_uses_temp_paths_ports_and_stays_valid(tmp_path):
    before = demo.DEMO_CONFIG.read_text(encoding="utf-8")
    raw = demo.build_runtime_config(demo.DEMO_CONFIG, gateway_port=8123, mock_port=9123, workdir=tmp_path, fast=False)
    cfg = AppConfig.model_validate(raw)
    assert cfg.gateway.port == 8123 and cfg.mock_upstream.port == 9123
    assert cfg.provider("mock_market").base_url == "http://127.0.0.1:9123"
    assert cfg.budget.path.startswith(str(tmp_path)) and cfg.recorder.path.startswith(str(tmp_path))
    assert cfg.provider("anthropic").enabled is False
    assert demo.DEMO_CONFIG.read_text(encoding="utf-8") == before  # o config da demo não é alterado
    fast = demo.build_runtime_config(demo.DEMO_CONFIG, gateway_port=1, mock_port=2, workdir=tmp_path, fast=True)
    assert demo.DemoSettings.from_config(AppConfig.model_validate(fast)).cooldown_s == demo.FAST_COOLDOWN_S


# --------------------------------------------------------------------------- Ollama
def tags_client(models: list[str], seen: list | None = None) -> httpx.AsyncClient:
    def handler(request: httpx.Request) -> httpx.Response:
        if seen is not None:
            seen.append((request.method, request.url.path))
        return httpx.Response(200, json={"models": [{"name": m} for m in models]})

    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def refusing_client() -> httpx.AsyncClient:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("recusado", request=request)

    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


async def test_check_ollama_passes_and_only_reads_the_tag_list():
    cfg = AppConfig.model_validate(demo.build_runtime_config(
        demo.DEMO_CONFIG, gateway_port=1, mock_port=2, workdir=Path("."), fast=True))
    seen: list = []
    async with tags_client(["qwen2.5:3b", "llama3.2:3b"], seen) as client:
        await demo.check_ollama(client, cfg)
    assert seen and set(seen) == {("GET", "/api/tags")}  # nunca baixa nada


async def test_check_ollama_absent_fails_with_clear_message():
    cfg = AppConfig.model_validate(demo.build_runtime_config(
        demo.DEMO_CONFIG, gateway_port=1, mock_port=2, workdir=Path("."), fast=True))
    async with refusing_client() as client:
        with pytest.raises(demo.DemoError, match="Ollama nao respondeu") as e:
            await demo.check_ollama(client, cfg)
    assert "ollama.com" in str(e.value)


async def test_check_ollama_missing_model_gives_pull_instruction():
    cfg = AppConfig.model_validate(demo.build_runtime_config(
        demo.DEMO_CONFIG, gateway_port=1, mock_port=2, workdir=Path("."), fast=True))
    async with tags_client(["qwen2.5:3b"]) as client:
        with pytest.raises(demo.DemoError) as e:
            await demo.check_ollama(client, cfg)
    assert "ollama pull llama3.2:3b" in str(e.value) and "nunca baixa" in str(e.value)


@pytest.mark.parametrize("client_factory,expected", [
    (refusing_client, "Ollama nao respondeu"),
    (lambda: tags_client(["qwen2.5:3b"]), "ollama pull llama3.2:3b"),
])
def test_main_fails_cleanly_before_starting_anything_when_ollama_is_not_ready(
        monkeypatch, capsys, client_factory, expected):
    def no_processes(*a, **k):
        raise AssertionError("nao deveria subir processos")

    monkeypatch.setattr(demo, "make_ollama_client", client_factory)
    monkeypatch.setattr(demo, "start_stack", no_processes)
    assert demo.main(["--no-color"]) == 2
    captured = capsys.readouterr()
    assert expected in captured.err and "Traceback" not in captured.err


def test_main_stops_the_stack_and_removes_the_temp_dir_on_ctrl_c(monkeypatch, capsys):
    events = {}

    @contextlib.contextmanager
    def fake_stack(config_path, workdir, gateway_port, mock_port):
        events["workdir"] = workdir
        events["config_exists"] = config_path.exists()
        try:
            yield _stack.Stack("http://127.0.0.1:1", "http://127.0.0.1:2")
        finally:
            events["stopped"] = True

    async def interrupted(*a, **k):
        raise KeyboardInterrupt

    monkeypatch.setattr(demo, "start_stack", fake_stack)
    monkeypatch.setattr(demo, "run_session", interrupted)
    assert demo.main(["--skip-ollama-check", "--no-color"]) == 130
    assert events["stopped"] and events["config_exists"]
    assert not events["workdir"].exists()  # diretório temporário removido
    assert "warm-up" in capsys.readouterr().out


# --------------------------------------------------------------------------- cenas
async def test_chaos_always_sends_the_complete_payload():
    sent = []

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(json.loads(request.content))
        return httpx.Response(200, json={})

    mock = httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="http://mock")
    d = demo.Demo(None, mock, demo.DemoSettings(10, 3, 10, 10))
    await d.chaos(mode="rate_limit", retry_after_seconds=7)
    await d.chaos()
    assert sent[0] == {**demo.CHAOS_DEFAULTS, "mode": "rate_limit", "retry_after_seconds": 7}
    assert all(set(p) == set(demo.CHAOS_DEFAULTS) for p in sent)


async def test_requests_use_short_generic_prompts_and_low_max_tokens():
    bodies = []

    def handler(request: httpx.Request) -> httpx.Response:
        bodies.append(json.loads(request.content))
        return httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}]})

    gw = httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="http://gw")
    d = demo.Demo(gw, None, demo.DemoSettings(10, 3, 10, 10))
    for _ in range(len(demo.PROMPTS) + 1):
        await d.ask()
    assert all(b["max_tokens"] <= 30 for b in bodies)
    assert all(len(b["messages"][0]["content"]) < 60 for b in bodies)
    assert demo.MAX_TOKENS <= 30 and all(len(p) < 60 for p in demo.PROMPTS)


async def test_scene_normal_is_served_by_the_mock(tmp_path):
    async with demo_env(tmp_path) as d:
        await d.scene_normal()
        assert [r.provider for r in d.rows] == ["mock_market"] * 3
        assert all(r.status == 200 and r.fallback == "" for r in d.rows)
        assert "recebidas=3" in "\n".join(d.test.lines)


async def test_scene_provider_rate_limit_falls_back_then_skips_by_cooldown(tmp_path):
    async with demo_env(tmp_path) as d:
        await d.scene_provider_rate_limit()
        assert [r.fallback for r in d.rows] == [
            "mock_market:rate_limited", "mock_market:cooling_down", "mock_market:cooling_down"]
        assert all(r.provider == "ollama_qwen" for r in d.rows)
        assert "recebeu 1 requisicao" in "\n".join(d.test.lines)  # o mock só viu a primeira
        assert d.test.sleeps and d.test.sleeps[-1] >= d.settings.retry_after_s - 1  # esperou o cooldown
        assert (await d.mock_market())["cooldown_remaining_s"] == 0


async def test_scene_provider_failure_opens_breaker_and_mock_stops_being_called(tmp_path):
    async with demo_env(tmp_path) as d:
        await d.scene_provider_failure()
        reasons = [r.fallback for r in d.rows]
        assert reasons == ["mock_market:server_error"] * 3 + ["mock_market:breaker_open"] * 2
        assert (await d.mock_market())["breaker"]["state"] == "open"
        assert (await d.stats())["received"] == 3
        assert "OK: o mock tinha recebido 3" in "\n".join(d.test.lines)


async def test_scene_recovery_goes_half_open_then_closed(tmp_path):
    async with demo_env(tmp_path) as d:
        await d.scene_provider_failure()
        before = len(d.rows)
        await d.scene_recovery()
        rows = d.rows[before:]
        assert [r.provider for r in rows] == ["ollama_qwen", "mock_market", "mock_market"]
        assert rows[0].fallback == "mock_market:breaker_open"
        out = "\n".join(d.test.lines)
        assert "breaker=half_open" in out and "depois da sonda: mock_market breaker=closed" in out
        assert (await d.mock_market())["breaker"]["state"] == "closed"


async def test_scene_local_limit_empties_the_bucket_then_falls_back(tmp_path):
    async with demo_env(tmp_path) as d:
        await d.scene_local_limit()
        assert [r.provider for r in d.rows].count("mock_market") == d.settings.rpm
        assert [r.fallback for r in d.rows[-2:]] == ["mock_market:rate_limit_local"] * 2
        assert all(r.provider == "ollama_qwen" for r in d.rows[-2:])
        assert (await d.stats())["received"] == d.settings.rpm  # o mock não viu as barradas


async def test_scene_all_unavailable_shows_503_with_reasons_per_provider(tmp_path):
    async with demo_env(tmp_path) as d:
        await d.scene_local_limit()  # esvazia o balde
        d.rows.clear()
        await d.scene_all_unavailable()
        assert [r.status for r in d.rows] == [503, 503]
        assert d.rows[0].providers == {"mock_market": "rate_limit_local"} and d.rows[0].retry_after
        assert d.rows[1].providers == {"anthropic": "disabled"}


async def test_full_run_summary_matches_requests_and_never_touches_anthropic(tmp_path):
    async with demo_env(tmp_path, fast=True) as d:
        await d.run()
        out = "\n".join(d.test.lines)
        assert out.count("=== Cena") == 6
        per_provider, total, _ = demo.parse_metrics((await d.gw.get("/metrics")).text)
        assert sum(per_provider.values()) == len(d.rows)
        assert per_provider["mock_market"] + per_provider["ollama_qwen"] + per_provider["none"] == len(d.rows)
        assert total == sum(1 for r in d.rows for _ in filter(None, r.fallback.split(",")))
        assert "Resumo (via GET /metrics)" in out
        assert d.test.anthropic_calls == []  # nenhuma conexão com a Anthropic
        assert (await d.status())["budget"]["used"] == 0 and (await d.status())["real_api_enabled"] is False
