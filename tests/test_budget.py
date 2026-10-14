"""Budget: contador persistente, fail-closed, seguro com concorrência e entre processos."""

import asyncio
import json
import subprocess
import sys
import threading
from pathlib import Path

import pytest

import gateway.budget as budget_mod
from gateway.budget import Budget
from scripts import reset_budget

ROOT = Path(__file__).resolve().parent.parent


def used_on_disk(path) -> int:
    return json.loads(Path(path).read_text(encoding="utf-8"))["used"]


def test_blocks_at_limit_and_never_resets(tmp_path):
    b = Budget(tmp_path / "b.json", limit=3)
    assert [b.reserve() for _ in range(5)] == [True, True, True, False, False]
    st = b.status()
    assert (st.used, st.limit, st.remaining) == (3, 3, 0)
    assert used_on_disk(b.path) == 3
    # Um novo objeto (novo processo) continua de onde parou: sem reset automático.
    assert Budget(b.path, limit=3).reserve() is False


def test_default_limit_is_30(tmp_path):
    assert Budget(tmp_path / "b.json").limit == 30


def test_missing_file_and_dir_are_created_at_zero(tmp_path):
    b = Budget(tmp_path / "novo" / "dir" / "b.json", limit=2)
    assert b.status().used == 0  # status não cria o arquivo
    assert not b.path.exists()
    b.ensure()
    assert used_on_disk(b.path) == 0
    assert b.reserve() is True and used_on_disk(b.path) == 1


@pytest.mark.parametrize("content", [
    "isso nao e json", "", "[]", "{}", '{"used": -1}', '{"used": "3"}', '{"used": true}', '{"used": 1.5}',
])
def test_corrupt_or_invalid_file_counts_as_exhausted_and_is_not_reset(tmp_path, content, caplog):
    path = tmp_path / "b.json"
    path.write_text(content, encoding="utf-8")
    b = Budget(path, limit=5)
    assert b.reserve() is False
    st = b.status()
    assert st.remaining == 0 and st.error
    assert path.read_text(encoding="utf-8") == content  # nunca reinicia em silêncio
    b.ensure()
    assert path.read_text(encoding="utf-8") == content
    assert "budget" in caplog.text


def test_unreadable_file_counts_as_exhausted(tmp_path):
    path = tmp_path / "b.json"
    path.mkdir()  # um diretório no lugar do arquivo: ilegível
    b = Budget(path, limit=5)
    assert b.reserve() is False
    assert b.status().remaining == 0 and b.status().error


def test_used_above_limit_is_exhausted(tmp_path):
    path = tmp_path / "b.json"
    path.write_text('{"used": 99}', encoding="utf-8")
    b = Budget(path, limit=5)
    assert b.reserve() is False
    assert b.status().remaining == 0


def test_threads_cannot_exceed_limit(tmp_path):
    b = Budget(tmp_path / "b.json", limit=5)
    results: list[bool] = []
    threads = [threading.Thread(target=lambda: results.append(b.reserve())) for _ in range(30)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert sum(results) == 5 and used_on_disk(b.path) == 5


async def test_concurrent_async_tasks_cannot_exceed_limit(tmp_path):
    b = Budget(tmp_path / "b.json", limit=4)
    results = await asyncio.gather(*[asyncio.to_thread(b.reserve) for _ in range(25)])
    assert sum(results) == 4 and used_on_disk(b.path) == 4


def test_processes_cannot_exceed_limit(tmp_path):
    path = tmp_path / "b.json"
    code = (
        "import sys; from gateway.budget import Budget; b = Budget(sys.argv[1], 15); "
        "print(sum(b.reserve() for _ in range(10)))"
    )
    procs = [
        subprocess.Popen([sys.executable, "-c", code, str(path)], cwd=ROOT, stdout=subprocess.PIPE, text=True)
        for _ in range(4)
    ]
    granted = sum(int(p.communicate()[0]) for p in procs)
    assert all(p.returncode == 0 for p in procs)
    assert granted == 15 and used_on_disk(path) == 15


def test_write_is_atomic_via_os_replace(tmp_path, monkeypatch):
    b = Budget(tmp_path / "b.json", limit=5)
    assert b.reserve()
    calls = []
    real_replace = budget_mod.os.replace

    def spy(src, dst):
        calls.append((Path(src), Path(dst)))
        assert Path(src).parent == Path(dst).parent and Path(src) != Path(dst)
        return real_replace(src, dst)

    monkeypatch.setattr(budget_mod.os, "replace", spy)
    assert b.reserve()
    assert len(calls) == 1 and calls[0][1] == b.path
    assert used_on_disk(b.path) == 2


def test_failed_replace_keeps_old_file_and_leaves_no_temp(tmp_path, monkeypatch):
    b = Budget(tmp_path / "b.json", limit=5)
    assert b.reserve()

    def boom(src, dst):
        raise OSError("disco cheio")

    monkeypatch.setattr(budget_mod.os, "replace", boom)
    assert b.reserve() is False  # não consegue gravar: fail-closed
    monkeypatch.undo()
    assert used_on_disk(b.path) == 1
    assert [p.name for p in tmp_path.iterdir() if p.suffix == ".tmp"] == []


# --------------------------------------------------------------------- reset_budget.py
def test_reset_requires_confirmation(tmp_path, capsys):
    b = Budget(tmp_path / "b.json", limit=5)
    [b.reserve() for _ in range(3)]
    assert reset_budget.main([], input_fn=lambda _: "nao", budget=b) == 1
    assert used_on_disk(b.path) == 3  # nada mudou
    assert reset_budget.main([], input_fn=lambda _: "", budget=b) == 1
    assert used_on_disk(b.path) == 3
    assert reset_budget.main([], input_fn=lambda _: "RESETAR", budget=b) == 0
    assert used_on_disk(b.path) == 0


def test_reset_yes_flag_skips_prompt(tmp_path):
    b = Budget(tmp_path / "b.json", limit=5)
    b.reserve()

    def no_prompt(_):
        raise AssertionError("não deveria perguntar")

    assert reset_budget.main(["--yes"], input_fn=no_prompt, budget=b) == 0
    assert used_on_disk(b.path) == 0
