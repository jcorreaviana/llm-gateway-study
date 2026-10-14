"""Reset MANUAL do orçamento de chamadas reais. Nunca é chamado automaticamente.

    python scripts/reset_budget.py            # pede confirmação (digite RESETAR)
    python scripts/reset_budget.py --yes      # sem prompt (uso consciente)
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from gateway.budget import Budget  # noqa: E402
from gateway.config import load_config, resolve_path  # noqa: E402


def main(argv: list[str] | None = None, input_fn=input, budget: Budget | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--yes", action="store_true", help="não pedir confirmação")
    args = ap.parse_args(argv)

    if budget is None:
        cfg = load_config().budget
        budget = Budget(resolve_path(cfg.path), cfg.limit)
    st = budget.status()
    print(f"Budget atual: usado={st.used} limite={st.limit} restante={st.remaining}")
    if st.error:
        print(f"Aviso: {st.error}")
    if not args.yes:
        answer = input_fn("Isto zera o contador de chamadas reais. Digite RESETAR para confirmar: ")
        if answer.strip() != "RESETAR":
            print("Cancelado. Nada foi alterado.")
            return 1
    with budget.locked():
        budget.write(0)
    print("Budget zerado.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
