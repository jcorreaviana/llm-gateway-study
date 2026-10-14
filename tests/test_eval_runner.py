"""Runner da avaliação (Fase 3). Sem rede externa e sem Ollama: o gateway roda em memória
(ASGITransport), o Ollama é httpx.MockTransport e os processos são simulados. O único teste com Ollama
real é `@pytest.mark.integration` (desligado por padrão)."""

import ast
import asyncio
import contextlib
import json
import re
import subprocess
import sys
from pathlib import Path

import httpx
import pytest
import yaml

from evalkit import run
from evalkit import validate_dataset as vd
from gateway.budget import Budget
from gateway.config import AppConfig, load_config
from gateway.main import create_app
from scripts import _stack

REPO_ROOT = Path(__file__).resolve().parent.parent
OLLAMA_URL = "http://localhost:11434"
MODELS = {"ollama_qwen": "qwen2.5:3b", "ollama_llama": "llama3.2:3b"}


def user_text(body: dict) -> str:
    return body["messages"][-1]["content"]


class Upstream:
    """Ollama simulado: guarda cada corpo recebido, mede a concorrência e falha sob demanda."""

    def __init__(self, fail_when=None, timeout_when=None):
        self.bodies: list[dict] = []
        self.in_flight = self.max_in_flight = 0
        self.fail_when, self.timeout_when = fail_when, timeout_when

    async def __call__(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        self.bodies.append(body)
        self.in_flight += 1
        self.max_in_flight = max(self.max_in_flight, self.in_flight)
        try:
            await asyncio.sleep(0)
            text = user_text(body)
            if self.timeout_when and self.timeout_when(text):
                raise httpx.ReadTimeout("lento demais", request=request)
            if self.fail_when and self.fail_when(text):
                return httpx.Response(500, json={"error": "falha simulada"})
            return httpx.Response(200, json={
                "model": body.get("model", "?"),
                "choices": [{"index": 0, "message": {"role": "assistant", "content": f"resp:{text[:12]}"},
                             "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 7, "completion_tokens": 3, "total_tokens": 10},
            })
        finally:
            self.in_flight -= 1


def build_app(tmp_path: Path, upstream: Upstream, provider: str = "ollama_qwen"):
    raw = run.build_runtime_config(run.EVAL_CONFIG, provider=provider, gateway_port=8000, mock_port=9001,
                                   workdir=tmp_path)
    budget = Budget(tmp_path / "budget.json", 30)
    budget.ensure()
    client = httpx.AsyncClient(mounts={OLLAMA_URL: httpx.MockTransport(upstream)})
    return create_app(AppConfig.model_validate(raw), client=client, budget=budget), raw


def gateway_client(app) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://gateway")


def read_jsonl(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


@pytest.fixture(scope="module")
def prompts():
    return vd.load_dataset().prompts


# --------------------------------------------------------------------------- coleta (collect)
async def test_collect_writes_one_result_per_prompt_in_sequence(tmp_path, prompts):
    upstream = Upstream()
    app, _ = build_app(tmp_path, upstream)
    out = tmp_path / "res" / "ollama_qwen.jsonl"
    async with gateway_client(app) as gw:
        records = await run.collect(gw, prompts, "ollama_qwen", out, resume=False, out=lambda s: None)
    saved = read_jsonl(out)
    assert [r["prompt_id"] for r in saved] == [p.id for p in prompts] and len(records) == 48
    assert len(upstream.bodies) == 49  # +1: o warm-up descartado, que não é gravado
    assert upstream.max_in_flight == 1  # sem concorrência
    first = saved[0]
    assert set(first) >= {"prompt_id", "task_type", "status", "response", "latency_ms", "usage", "request_id"}
    assert first["status"] == 200 and first["response"].startswith("resp:") and first["usage"]["total_tokens"] == 10
    assert re.fullmatch(r"[0-9a-f-]{36}", first["request_id"]) and "error" not in first
    assert all(r["task_type"] == p.task_type for r, p in zip(saved, prompts, strict=True))


async def test_collect_sends_forced_provider_temperature_zero_and_the_prompt_max_tokens(tmp_path, prompts):
    upstream = Upstream()
    app, _ = build_app(tmp_path, upstream, "ollama_llama")
    seen = {}

    async def spy(request: httpx.Request) -> httpx.Response:
        seen.setdefault("headers", []).append(request.headers.get("x-gateway-force-provider"))
        return await gw_transport.handle_async_request(request)

    gw_transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=httpx.MockTransport(spy), base_url="http://gateway") as gw:
        await run.collect(gw, prompts[:2], "ollama_llama", tmp_path / "o.jsonl", resume=False, out=lambda s: None)
    assert set(seen["headers"]) == {"ollama_llama"}
    real = upstream.bodies[1:]  # o primeiro é o warm-up
    assert [b["max_tokens"] for b in real] == [p.params.max_tokens for p in prompts[:2]]
    assert all(b["temperature"] == 0 and b["model"] == "llama3.2:3b" for b in real)


async def test_one_failing_prompt_does_not_stop_the_run(tmp_path, prompts):
    upstream = Upstream(fail_when=lambda t: "Zeta" in t, timeout_when=lambda t: "torneira" in t)
    app, _ = build_app(tmp_path, upstream)
    out = tmp_path / "o.jsonl"
    async with gateway_client(app) as gw:
        await run.collect(gw, prompts, "ollama_qwen", out, resume=False, out=lambda s: None)
    saved = {r["prompt_id"]: r for r in read_jsonl(out)}
    assert len(saved) == 48
    bad = [r for r in saved.values() if r.get("error")]
    assert {r["prompt_id"] for r in bad} == {"extraction-06", "reasoning-07"}
    for r in bad:
        assert r["status"] == 503 and r["response"] is None and r["latency_ms"] >= 0 and r["request_id"]
    assert sum(1 for r in saved.values() if r["status"] == 200) == 46


async def test_transport_errors_become_results_with_error(tmp_path, prompts):
    def boom(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("sem resposta", request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(boom), base_url="http://gateway") as gw:
        records = await run.collect(gw, prompts[:2], "ollama_qwen", tmp_path / "o.jsonl", resume=False,
                                    out=lambda s: None)
    assert len(records) == 2
    assert all(r["status"] == 0 and r["response"] is None and r["error"].startswith("ReadTimeout") for r in records)
    assert run.summarize(records) == {"total": 2, "ok": 0, "errors": 2, "latency_p50_ms": None}


async def test_resume_skips_prompts_already_recorded(tmp_path, prompts):
    out = tmp_path / "o.jsonl"
    first = Upstream()
    app, _ = build_app(tmp_path, first)
    async with gateway_client(app) as gw:
        await run.collect(gw, prompts[:3], "ollama_qwen", out, resume=False, out=lambda s: None)
    second = Upstream()
    app2, _ = build_app(tmp_path / "2", second)
    lines: list[str] = []
    async with gateway_client(app2) as gw:
        records = await run.collect(gw, prompts[:5], "ollama_qwen", out, resume=True, out=lines.append)
    saved = read_jsonl(out)
    assert [r["prompt_id"] for r in saved] == [p.id for p in prompts[:5]]  # sem duplicatas
    assert len(records) == 5
    assert [user_text(b) for b in second.bodies[1:]] == [user_text(p.request_body()) for p in prompts[3:5]]
    assert any("--resume: 3 prompt(s)" in s for s in lines)


async def test_resume_with_everything_done_calls_nothing(tmp_path, prompts):
    out = tmp_path / "o.jsonl"
    app, _ = build_app(tmp_path, Upstream())
    async with gateway_client(app) as gw:
        await run.collect(gw, prompts[:2], "ollama_qwen", out, resume=False, out=lambda s: None)
    idle = Upstream()
    app2, _ = build_app(tmp_path / "2", idle)
    async with gateway_client(app2) as gw:
        records = await run.collect(gw, prompts[:2], "ollama_qwen", out, resume=True, out=lambda s: None)
    assert len(records) == 2 and idle.bodies == []


async def test_resume_discards_a_truncated_last_line(tmp_path, prompts):
    out = tmp_path / "o.jsonl"
    good_line = json.dumps({"prompt_id": prompts[0].id, "status": 200, "latency_ms": 5, "response": "x"})
    out.write_text(good_line + '\n{"prompt_id": "' + prompts[1].id + '", "sta', encoding="utf-8")
    app, _ = build_app(tmp_path, Upstream())
    async with gateway_client(app) as gw:
        await run.collect(gw, prompts[:2], "ollama_qwen", out, resume=True, out=lambda s: None)
    saved = read_jsonl(out)  # todas as linhas são JSON válido de novo
    assert [r["prompt_id"] for r in saved] == [prompts[0].id, prompts[1].id]


async def test_ask_handles_gateway_error_bodies(prompts):
    def err(status, body):
        return lambda request: httpx.Response(status, json=body, headers={"x-gateway-request-id": "rid"})

    cases = [
        (err(503, {"error": {"message": "indisponível", "providers": {"ollama_qwen": "timeout"}}}), "indisponível"),
        (err(400, {"error": {"message": "provedor desconhecido"}}), "provedor desconhecido"),
        (lambda r: httpx.Response(502, text="bad gateway"), "bad gateway"),
        (lambda r: httpx.Response(200, json={"choices": []}), "sem choices"),
    ]
    for handler, expected in cases:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="http://g") as gw:
            rec = await run.ask(gw, prompts[0], "ollama_qwen", lambda: 0.0)
        assert expected in rec["error"] and rec["response"] is None


async def test_ask_flags_an_answer_from_another_provider(prompts):
    def handler(request):
        return httpx.Response(200, headers={"x-gateway-provider": "ollama_llama"}, json={
            "choices": [{"message": {"role": "assistant", "content": "oi"}}]})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="http://g") as gw:
        rec = await run.ask(gw, prompts[0], "ollama_qwen", lambda: 0.0)
    assert "outro provedor" in rec["error"]


# --------------------------------------------------------------------------- seleção e resumo
def test_select_prompts_alternates_categories_and_keeps_dataset_order(prompts):
    chosen = run.select_prompts(prompts, 3)
    assert {p.task_type for p in chosen} == {"extraction", "classification", "reasoning"}
    assert [p.id for p in chosen] == [p.id for p in prompts if p.id in {c.id for c in chosen}]
    assert len(run.select_prompts(prompts, 7)) == 7
    assert run.select_prompts(prompts, None) == prompts and len(run.select_prompts(prompts, 500)) == 48
    big = {p.id for p in run.select_prompts(prompts, 12)}
    assert all(sum(1 for p in prompts if p.id in big and p.task_type == t) == 2 for t in {p.task_type for p in prompts})


def test_summarize_and_format():
    records = [{"status": 200, "latency_ms": 100}, {"status": 200, "latency_ms": 300}, {"status": 503, "latency_ms": 9, "error": "x"}]
    s = run.summarize(records)
    assert s == {"total": 3, "ok": 2, "errors": 1, "latency_p50_ms": 200}
    assert run.format_summary("ollama_qwen", "qwen2.5:3b", s) == (
        "ollama_qwen (qwen2.5:3b): 3 resultados, 2 ok, 1 com erro, latência p50 200 ms")


# --------------------------------------------------------------------------- execute (fluxo completo)
class Harness:
    """Troca Ollama, pilha de processos e cliente do gateway por versões em memória."""

    def __init__(self, monkeypatch, tmp_path, *, models=("qwen2.5:3b", "llama3.2:3b"), version="0.9.9",
                 upstream=None, ollama_up=True):
        self.tmp_path, self.upstream = tmp_path, upstream or Upstream()
        self.requests: list[tuple[str, str]] = []
        self.stack_args: list[tuple] = []
        self.runtime_config: dict = {}
        self.stopped = 0
        self.app = None

        def tags(request: httpx.Request) -> httpx.Response:
            self.requests.append((request.method, request.url.path))
            if not ollama_up:
                raise httpx.ConnectError("recusado", request=request)
            if request.url.path == "/api/version":
                return httpx.Response(200, json={"version": version})
            return httpx.Response(200, json={"models": [{"name": m} for m in models]})

        @contextlib.contextmanager
        def fake_stack(config_path, workdir, gateway_port, mock_port):
            self.stack_args.append((config_path, workdir, gateway_port, mock_port))
            self.runtime_config = yaml.safe_load(config_path.read_text(encoding="utf-8"))
            cfg = AppConfig.model_validate(self.runtime_config)
            budget = Budget(workdir / "budget.json", 30)
            budget.ensure()
            self.app = create_app(
                cfg, client=httpx.AsyncClient(mounts={OLLAMA_URL: httpx.MockTransport(self.upstream)}), budget=budget)
            try:
                yield _stack.Stack(f"http://127.0.0.1:{mock_port}", f"http://127.0.0.1:{gateway_port}")
            finally:
                self.stopped += 1

        monkeypatch.setenv("ENABLE_REAL_API", "0")  # registra para o monkeypatch restaurar o que o runner mexer
        monkeypatch.setenv("ANTHROPIC_API_KEY", "")
        monkeypatch.setattr(run, "make_ollama_client", lambda: httpx.AsyncClient(transport=httpx.MockTransport(tags)))
        monkeypatch.setattr(run, "start_stack", fake_stack)
        monkeypatch.setattr(run, "make_gateway_client", lambda url: gateway_client(self.app))

    def options(self, provider="ollama_qwen", run_id="r1", **kw) -> run.Options:
        return run.Options(provider=provider, run_id=run_id, results_root=self.tmp_path / "results", **kw)


@pytest.fixture
def harness(monkeypatch, tmp_path):
    return Harness(monkeypatch, tmp_path)


def test_execute_runs_the_whole_flow_and_writes_results_and_meta(harness):
    lines: list[str] = []
    summary = run.execute(harness.options(limit=6), out=lines.append)
    run_dir = harness.tmp_path / "results" / "r1"
    results = read_jsonl(run_dir / "ollama_qwen.jsonl")
    assert len(results) == 6 and summary == {"total": 6, "ok": 6, "errors": 0, "latency_p50_ms": summary["latency_p50_ms"]}
    assert harness.stopped == 1
    assert "ollama_qwen (qwen2.5:3b): 6 resultados, 6 ok, 0 com erro" in lines[-1]

    meta = json.loads((run_dir / "meta.json").read_text(encoding="utf-8"))
    assert meta["run_id"] == "r1" and re.fullmatch(r"\d{4}-\d\d-\d\dT[\d:]+\+00:00", meta["created_at"])
    assert meta["ollama_version"] == "0.9.9"
    assert meta["dataset_hash"] == vd.dataset_hash() and meta["config_hash"] == run.file_hash(run.EVAL_CONFIG)
    assert re.fullmatch(r"[0-9a-f]{40}", meta["git_commit"]) and isinstance(meta["git_dirty"], bool)
    entry = meta["providers"]["ollama_qwen"]
    assert entry["model"] == "qwen2.5:3b" and entry["prompts_selected"] == 6 and entry["results"] == 6
    assert entry["limit"] == 6 and entry["started_at"] and entry["finished_at"]
    assert "NÃO mede a perda" in meta["scope"] and "Anthropic" in meta["scope"]


def test_execute_uses_a_derived_config_with_only_the_measured_provider(harness):
    run.execute(harness.options(provider="ollama_llama", limit=1), out=lambda s: None)
    cfg = harness.runtime_config
    assert cfg["chain"] == ["ollama_llama"]
    by_name = {p["name"]: p for p in cfg["providers"]}
    assert by_name["ollama_llama"]["warmup"] is True and by_name["ollama_qwen"]["warmup"] is False  # não carrega o outro modelo
    assert by_name["anthropic"]["enabled"] is False
    config_path, workdir, gateway_port, mock_port = harness.stack_args[0]
    assert gateway_port != mock_port and cfg["gateway"]["port"] == gateway_port and cfg["mock_upstream"]["port"] == mock_port
    run_dir = harness.tmp_path / "results" / "r1"
    assert workdir == run_dir / "gateway" / "ollama_llama"
    for path in (cfg["budget"]["path"], cfg["recorder"]["path"]):
        assert Path(path).is_relative_to(run_dir)  # nada em data/ nem logs/ do repositório
    assert load_config(run.EVAL_CONFIG).chain == ["ollama_qwen", "ollama_llama"]  # o arquivo original não muda


def test_two_providers_share_a_run_id_and_meta(harness):
    run.execute(harness.options("ollama_qwen", limit=2), out=lambda s: None)
    run.execute(harness.options("ollama_llama", limit=2), out=lambda s: None)
    run_dir = harness.tmp_path / "results" / "r1"
    meta = json.loads((run_dir / "meta.json").read_text(encoding="utf-8"))
    assert set(meta["providers"]) == {"ollama_qwen", "ollama_llama"}
    assert meta["providers"]["ollama_llama"]["model"] == "llama3.2:3b"
    assert (run_dir / "ollama_qwen.jsonl").exists() and (run_dir / "ollama_llama.jsonl").exists()


def test_execute_resume_skips_what_is_recorded(harness):
    run.execute(harness.options(limit=3), out=lambda s: None)
    before = len(harness.upstream.bodies)
    with pytest.raises(run.PreflightError, match="--resume"):
        run.execute(harness.options(limit=6), out=lambda s: None)  # sem --resume não sobrescreve nada
    run.execute(harness.options(limit=6, resume=True), out=lambda s: None)
    results = read_jsonl(harness.tmp_path / "results" / "r1" / "ollama_qwen.jsonl")
    assert len({r["prompt_id"] for r in results}) == len(results) == 6
    assert len(harness.upstream.bodies) - before == 3 + 1  # 3 restantes + warm-up
    meta = json.loads((harness.tmp_path / "results" / "r1" / "meta.json").read_text(encoding="utf-8"))
    assert meta["providers"]["ollama_qwen"]["resumed"] is True


def test_a_failing_prompt_does_not_break_execute(monkeypatch, tmp_path):
    h = Harness(monkeypatch, tmp_path, upstream=Upstream(fail_when=lambda t: "Zeta" in t))
    summary = run.execute(h.options(), out=lambda s: None)
    assert summary["total"] == 48 and summary["errors"] == 1 and summary["ok"] == 47
    assert h.stopped == 1


def test_changed_dataset_or_config_in_the_same_run_is_refused(harness):
    run.execute(harness.options("ollama_qwen", limit=1), out=lambda s: None)
    meta_path = harness.tmp_path / "results" / "r1" / "meta.json"
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    meta["dataset_hash"] = "0" * 64
    meta_path.write_text(json.dumps(meta), encoding="utf-8")
    with pytest.raises(run.PreflightError, match="outro dataset ou config"):
        run.execute(harness.options("ollama_llama", limit=1), out=lambda s: None)


# --------------------------------------------------------------------------- pré-checagens
def eval_config_variant(tmp_path: Path, mutate) -> Path:
    raw = yaml.safe_load(run.EVAL_CONFIG.read_text(encoding="utf-8"))
    mutate(raw)
    path = tmp_path / "variant.yaml"
    path.write_text(yaml.safe_dump(raw, allow_unicode=True), encoding="utf-8")
    return path


def test_refuses_to_run_with_anthropic_in_the_chain(harness):
    path = eval_config_variant(harness.tmp_path, lambda raw: raw["chain"].append("anthropic"))
    with pytest.raises(run.PreflightError, match="anthropic está na chain"):
        run.execute(harness.options(config_path=path), out=lambda s: None)
    assert harness.stack_args == [] and harness.requests == []  # nem Ollama nem processos


def test_refuses_to_run_with_anthropic_enabled(harness):
    def enable(raw):
        next(p for p in raw["providers"] if p["name"] == "anthropic")["enabled"] = True

    with pytest.raises(run.PreflightError, match="anthropic está habilitado"):
        run.execute(harness.options(config_path=eval_config_variant(harness.tmp_path, enable)), out=lambda s: None)
    assert harness.stack_args == []


def test_derived_config_never_carries_anthropic_in_the_chain(tmp_path):
    raw = run.build_runtime_config(run.EVAL_CONFIG, provider="ollama_qwen", gateway_port=1, mock_port=2, workdir=tmp_path)
    cfg = AppConfig.model_validate(raw)
    assert cfg.chain == ["ollama_qwen"] and cfg.provider("anthropic").enabled is False
    bad = eval_config_variant(tmp_path, lambda r: next(p for p in r["providers"] if p["name"] == "anthropic").update(enabled=True))
    with pytest.raises(run.PreflightError):
        run.build_runtime_config(bad, provider="ollama_qwen", gateway_port=1, mock_port=2, workdir=tmp_path)


def test_refuses_when_real_api_is_enabled_in_the_environment(harness, monkeypatch):
    monkeypatch.setenv("ENABLE_REAL_API", "1")
    with pytest.raises(run.PreflightError, match="ENABLE_REAL_API"):
        run.execute(harness.options(), out=lambda s: None)
    assert harness.stack_args == []


def test_guard_environment_clears_the_key_and_forces_the_flag_off(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "valor-do-ambiente-pai")
    monkeypatch.delenv("ENABLE_REAL_API", raising=False)
    run.guard_environment()
    assert run.os.environ["ENABLE_REAL_API"] == "0" and run.os.environ["ANTHROPIC_API_KEY"] == ""
    monkeypatch.delenv("ENABLE_REAL_API")  # o guard recriou; deixa o monkeypatch limpar no fim


def test_child_processes_get_real_api_off_and_an_empty_key(tmp_path, monkeypatch):
    """Pilha real (start_stack), com Popen simulado: o que importa é o ambiente dos filhos."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "valor-do-ambiente-pai")
    monkeypatch.setenv("ENABLE_REAL_API", "0")
    launched = []

    class FakeProc:
        returncode = 1

        def __init__(self, cmd, **kw):
            launched.append((cmd, kw))

        def poll(self):
            return 1

    h = Harness(monkeypatch, tmp_path)
    monkeypatch.setattr(run, "start_stack", _stack.start_stack)  # a de verdade
    monkeypatch.setattr(run, "git_info", lambda: {"git_commit": None, "git_dirty": None})
    monkeypatch.setattr(_stack.subprocess, "Popen", FakeProc)
    with pytest.raises(_stack.StackError, match="encerrou antes do /health"):
        run.execute(h.options(limit=1), out=lambda s: None)
    assert len(launched) == 2
    for _cmd, kw in launched:
        assert kw["env"]["ENABLE_REAL_API"] == "0" and kw["env"]["ANTHROPIC_API_KEY"] == ""
        assert "valor-do-ambiente-pai" not in kw["env"].values()
    gateway_cmd = launched[1][0]
    assert gateway_cmd[gateway_cmd.index("--host") + 1] == "127.0.0.1"
    assert launched[1][1]["env"]["GATEWAY_CONFIG"].endswith("config.runtime.yaml")


def test_refuses_when_ollama_is_down_and_never_pulls(monkeypatch, tmp_path):
    h = Harness(monkeypatch, tmp_path, ollama_up=False)
    with pytest.raises(run.PreflightError, match="Ollama não respondeu"):
        run.execute(h.options(), out=lambda s: None)
    assert h.stack_args == []


def test_refuses_when_the_model_is_missing_and_only_reads(monkeypatch, tmp_path):
    h = Harness(monkeypatch, tmp_path, models=("llama3.2:3b",))
    with pytest.raises(run.PreflightError, match="ollama pull qwen2.5:3b") as e:
        run.execute(h.options(), out=lambda s: None)
    assert "nunca baixa" in str(e.value)
    assert h.requests and {m for m, _ in h.requests} == {"GET"}
    assert {p for _, p in h.requests} <= {"/api/version", "/api/tags"}
    assert h.stack_args == []


def test_accepts_the_latest_tag_of_an_installed_model(monkeypatch, tmp_path):
    h = Harness(monkeypatch, tmp_path, models=("qwen2.5:3b:latest",))
    assert run.execute(h.options(limit=1), out=lambda s: None)["total"] == 1


def test_refuses_an_invalid_dataset(harness, tmp_path):
    bad = tmp_path / "ds"
    bad.mkdir()
    (bad / "a.jsonl").write_text("{}\n", encoding="utf-8")
    with pytest.raises(run.PreflightError, match="dataset inválido"):
        run.execute(harness.options(dataset_dir=bad), out=lambda s: None)
    assert harness.stack_args == [] and harness.requests == []


@pytest.mark.parametrize("provider,fragment", [
    ("anthropic", "não está na chain"),
    ("inexistente", "não está na chain"),
])
def test_refuses_unknown_or_non_chain_providers(harness, provider, fragment):
    with pytest.raises(run.PreflightError, match=fragment):
        run.execute(harness.options(provider=provider), out=lambda s: None)


def test_refuses_force_provider_disabled(harness):
    path = eval_config_variant(harness.tmp_path, lambda raw: raw["gateway"].update(force_provider_enabled=False))
    with pytest.raises(run.PreflightError, match="force_provider_enabled"):
        run.execute(harness.options(config_path=path), out=lambda s: None)


@pytest.mark.parametrize("run_id", ["", "../fora", "a/b", "com espaço", "x" * 65, ".oculto"])
def test_refuses_unsafe_run_ids(harness, run_id):
    with pytest.raises(run.PreflightError, match="run-id"):
        run.execute(harness.options(run_id=run_id), out=lambda s: None)


def test_refuses_an_invalid_config_file(harness, tmp_path):
    bad = tmp_path / "bad.yaml"
    bad.write_text("chain: []\nproviders: []\n", encoding="utf-8")
    with pytest.raises(run.PreflightError, match="config de avaliação inválido"):
        run.execute(harness.options(config_path=bad), out=lambda s: None)


# --------------------------------------------------------------------------- main
def test_main_reports_errors_cleanly(monkeypatch, capsys):
    def boom(opts, out=print):
        raise run.PreflightError("tudo errado")

    monkeypatch.setattr(run, "execute", boom)
    assert run.main(["--provider", "ollama_qwen", "--run-id", "x"]) == 2
    err = capsys.readouterr().err
    assert "erro: tudo errado" in err and "Traceback" not in err


def test_main_handles_ctrl_c_and_bad_limit(monkeypatch, capsys):
    def interrupted(opts, out=print):
        raise KeyboardInterrupt

    monkeypatch.setattr(run, "execute", interrupted)
    assert run.main(["--provider", "ollama_qwen", "--run-id", "x"]) == 130
    assert "--resume" in capsys.readouterr().err
    assert run.main(["--provider", "ollama_qwen", "--run-id", "x", "--limit", "0"]) == 2


def test_main_passes_the_options_through(monkeypatch):
    seen = {}

    def fake(opts, out=print):
        seen["opts"] = opts
        return {}

    monkeypatch.setattr(run, "execute", fake)
    assert run.main(["--provider", "ollama_llama", "--run-id", "r9", "--resume", "--limit", "5"]) == 0
    o = seen["opts"]
    assert (o.provider, o.run_id, o.resume, o.limit) == ("ollama_llama", "r9", True, 5)


# --------------------------------------------------------------------------- nada de Anthropic
FORBIDDEN_TEXT = ("api.anthropic.com", "AnthropicProvider", "x-api-key", "anthropic-version")


def evalkit_sources() -> list[Path]:
    return sorted((REPO_ROOT / "evalkit").glob("*.py"))


def test_runner_does_not_import_or_reference_any_anthropic_client():
    for path in evalkit_sources():
        text = path.read_text(encoding="utf-8")
        for token in FORBIDDEN_TEXT:
            assert token not in text, f"{path.name} referencia {token}"
        for node in ast.walk(ast.parse(text)):
            names = []
            if isinstance(node, ast.Import):
                names = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom):
                names = [node.module or ""]
            for name in names:
                assert not name.startswith("anthropic"), f"{path.name} importa {name}"
                assert name != "gateway.providers", f"{path.name} importa o módulo que define o cliente da Anthropic"


def test_importing_the_runner_does_not_load_the_anthropic_client_module():
    code = ("import sys, evalkit.run, evalkit.validate_dataset\n"
            "bad = [m for m in sys.modules if m.startswith('anthropic') or m == 'gateway.providers']\n"
            "print(bad)\n")
    out = subprocess.run([sys.executable, "-c", code], cwd=REPO_ROOT, capture_output=True, text=True, timeout=60)
    assert out.returncode == 0, out.stderr
    assert out.stdout.strip() == "[]"


# --------------------------------------------------------------------------- integração (Ollama real)
def ollama_or_skip() -> None:
    try:
        tags = httpx.get(f"{OLLAMA_URL}/api/tags", timeout=3).json()
    except (httpx.HTTPError, ValueError) as e:
        pytest.skip(f"Ollama indisponível em {OLLAMA_URL} ({type(e).__name__}): suba o Ollama para rodar -m integration")
    names = {m.get("name") for m in tags.get("models", [])}
    for model in MODELS.values():
        if model not in names:
            pytest.skip(f"modelo {model} não instalado no Ollama (rode: ollama pull {model})")


@pytest.mark.integration
@pytest.mark.parametrize("provider", ["ollama_qwen", "ollama_llama"])
def test_real_ollama_runs_two_prompts_per_provider(provider, tmp_path, monkeypatch):
    ollama_or_skip()
    monkeypatch.setenv("ENABLE_REAL_API", "0")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "")
    opts = run.Options(provider=provider, run_id="it", limit=2, results_root=tmp_path / "results")
    summary = run.execute(opts, out=lambda s: None)
    assert summary["total"] == 2 and summary["ok"] == 2 and summary["latency_p50_ms"] > 0
    results = read_jsonl(tmp_path / "results" / "it" / f"{provider}.jsonl")
    assert all(r["status"] == 200 and r["response"] and r["usage"]["total_tokens"] > 0 for r in results)
    meta = json.loads((tmp_path / "results" / "it" / "meta.json").read_text(encoding="utf-8"))
    assert meta["providers"][provider]["model"] == MODELS[provider] and meta["ollama_version"]
