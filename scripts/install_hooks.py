"""Instala o hook `pre-commit` local que roda scripts/check_secrets.py.

    python scripts/install_hooks.py

O script é versionado; o hook instalado (em .git/hooks) não é. Se o scan falhar, o commit é
bloqueado. Rode de novo para reinstalar (sobrescreve o hook anterior).
"""
from __future__ import annotations

import stat
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

HOOK = """#!/bin/sh
# Instalado por scripts/install_hooks.py. Bloqueia o commit se o scan de segredos falhar.
cd "$(git rev-parse --show-toplevel)" || exit 1
PY={python}
[ -x "$PY" ] || PY=python
"$PY" scripts/check_secrets.py || {{
    echo "pre-commit: check_secrets.py falhou; commit bloqueado." >&2
    exit 1
}}
"""


def hooks_dir() -> Path:
    out = subprocess.run(
        ["git", "rev-parse", "--git-path", "hooks"],
        cwd=ROOT, capture_output=True, text=True, check=True,
    ).stdout.strip()
    path = Path(out)
    return path if path.is_absolute() else ROOT / path


def install() -> Path:
    target = hooks_dir() / "pre-commit"
    target.parent.mkdir(parents=True, exist_ok=True)
    # Caminho do Python com barras normais, para o sh do Git for Windows.
    python = '"' + Path(sys.executable).as_posix() + '"'
    target.write_text(HOOK.format(python=python), encoding="utf-8", newline="\n")
    target.chmod(target.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return target


if __name__ == "__main__":
    print(f"hook instalado em {install().relative_to(ROOT)}")
