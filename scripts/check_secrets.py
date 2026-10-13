"""Falha (exit 1) se achar padrões de segredo, e-mail pessoal ou caminho absoluto do Windows.

Uso:
    python scripts/check_secrets.py            # arquivos versionados + novos não ignorados
    python scripts/check_secrets.py --history  # também varre o histórico (git log -p --all)

Por segurança, a saída mostra só arquivo/linha e o NOME do padrão, nunca o valor encontrado.
"""
from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# Este script contém os próprios padrões, então ele se exclui da varredura.
SELF = "scripts/check_secrets.py"

# Padrões montados por partes para que o código-fonte não contenha nada com "cara de chave".
PATTERNS: dict[str, re.Pattern[str]] = {
    "chave Anthropic": re.compile("sk-" + r"ant-[A-Za-z0-9_\-]{8,}"),
    "chave AWS": re.compile("AK" + r"IA[0-9A-Z]{16}"),
    "token GitHub": re.compile(r"gh[pousr]_" + r"[A-Za-z0-9]{30,}"),
    "api_key literal": re.compile(r"api_key\s*=\s*[\"'][^\"']+"),
    # Letra de drive + barra invertida + pelo menos um diretório: C:\pasta\...
    "caminho absoluto Windows": re.compile(r"(?<![A-Za-z0-9])[A-Za-z]:\\{1,2}[\w .-]+\\"),
    "caminho absoluto Windows (/)": re.compile(r"(?<![A-Za-z0-9])[A-Za-z]:/(?:Users|Documents and Settings)/", re.I),
    "e-mail pessoal": re.compile(r"[\w.+-]+@(?:gmail|hotmail|outlook|yahoo)\.com", re.I),
}


def scan_line(line: str) -> list[str]:
    """Devolve os nomes dos padrões que casam com a linha."""
    return [name for name, rx in PATTERNS.items() if rx.search(line)]


def tracked_files() -> list[str]:
    """Arquivos versionados + novos que o .gitignore não cobre (o que entraria no próximo commit)."""
    out = subprocess.run(
        ["git", "ls-files", "-z", "--cached", "--others", "--exclude-standard"],
        cwd=ROOT, capture_output=True, check=True,
    ).stdout.decode("utf-8", "replace")
    return [f for f in out.split("\0") if f]


def scan_files() -> list[str]:
    findings = []
    for rel in tracked_files():
        if rel == SELF:
            continue
        path = ROOT / rel
        if not path.is_file():
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue  # binário ou ilegível: ignora
        for n, line in enumerate(text.splitlines(), 1):
            for name in scan_line(line):
                findings.append(f"{rel}:{n}: {name}")
    return findings


def scan_history() -> list[str]:
    """Varre as linhas adicionadas em todo o histórico: segredo removido depois continua público."""
    proc = subprocess.run(
        ["git", "log", "-p", "--all", "--no-color", "--format=commit %H"],
        cwd=ROOT, capture_output=True, check=True,
    )
    findings = []
    commit, current = "?", ""
    for raw in proc.stdout.decode("utf-8", "replace").splitlines():
        if raw.startswith("commit "):
            commit = raw[7:15]
        elif raw.startswith("+++ "):
            current = raw[6:] if raw.startswith("+++ b/") else raw[4:]
        elif raw.startswith("+") and current != SELF:
            for name in scan_line(raw[1:]):
                findings.append(f"commit {commit} {current}: {name}")
    return findings


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--history", action="store_true", help="varre também o histórico do git")
    args = ap.parse_args()

    findings = scan_files()
    if args.history:
        findings += scan_history()

    if findings:
        print("POSSÍVEIS SEGREDOS/DADOS PESSOAIS ENCONTRADOS (valores omitidos):")
        for f in findings:
            print(f"  {f}")
        return 1
    print("OK: nenhum padrão sensível encontrado" + (" (arquivos + histórico)." if args.history else "."))
    return 0


if __name__ == "__main__":
    sys.exit(main())
