"""Testes do check_secrets. Todas as "chaves" são montadas em tempo de execução,
para que nenhum literal com cara de segredo exista no repositório."""
import importlib.util
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "check_secrets.py"
spec = importlib.util.spec_from_file_location("check_secrets", SCRIPT)
cs = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cs)

FAKE_ANTHROPIC = "sk-" + "ant-" + "x" * 20
FAKE_AWS = "AK" + "IA" + "X" * 16
FAKE_WIN_PATH = "C" + ":" + "\\" + "pasta" + "\\" + "arquivo.txt"
FAKE_EMAIL = "fulano" + "@" + "gmail" + ".com"


def test_detects_each_pattern():
    assert "chave Anthropic" in cs.scan_line(f"x = {FAKE_ANTHROPIC}")
    assert "chave AWS" in cs.scan_line(FAKE_AWS)
    assert "api_key literal" in cs.scan_line("api_key" + ' = "' + "valor-falso" + '"')
    assert "caminho absoluto Windows" in cs.scan_line(f"open({FAKE_WIN_PATH!r})")
    assert "e-mail pessoal" in cs.scan_line(FAKE_EMAIL)


def test_clean_lines_pass():
    assert cs.scan_line("ANTHROPIC_API_KEY=coloque_sua_chave_aqui") == []
    assert cs.scan_line("caminho = Path('data') / 'budget.json'") == []
    assert cs.scan_line("id+usuario@users.noreply.github.com") == []


def test_main_fails_and_hides_value(tmp_path, monkeypatch, capsys):
    (tmp_path / "ruim.txt").write_text(FAKE_ANTHROPIC, encoding="utf-8")
    monkeypatch.setattr(cs, "ROOT", tmp_path)
    monkeypatch.setattr(cs, "tracked_files", lambda: ["ruim.txt"])
    monkeypatch.setattr("sys.argv", ["check_secrets"])
    assert cs.main() == 1
    out = capsys.readouterr().out
    assert "ruim.txt:1" in out
    assert FAKE_ANTHROPIC not in out  # nunca imprime o valor


def test_script_excludes_itself(tmp_path, monkeypatch):
    # Um arquivo com o caminho do próprio script é ignorado, mesmo com conteúdo suspeito.
    (tmp_path / "scripts").mkdir()
    (tmp_path / cs.SELF).write_text(FAKE_ANTHROPIC, encoding="utf-8")
    monkeypatch.setattr(cs, "ROOT", tmp_path)
    monkeypatch.setattr(cs, "tracked_files", lambda: [cs.SELF])
    assert cs.scan_files() == []
