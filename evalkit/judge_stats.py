"""Estatística da calibração do juiz (Marco 11), sem dependências (só a biblioteca padrão).

CRITÉRIO DE DECISÃO (definido ANTES de ver os números do run completo e não ajustado depois): o juiz só pode ser
usado nas tarefas abertas, com ressalva, se no conjunto VERIFICÁVEL (todas as categorias com `check`) o veredito
dele (nota geral >= `APPROVE_MIN`) concordar com o `passed` do scorer determinístico com kappa de Cohen
>= `KAPPA_MIN` E acurácia >= `ACCURACY_MIN`. Abaixo disso, as notas dele aparecem no relatório como "não
confiáveis". A decisão é por juiz.
"""

from __future__ import annotations

import math
import random
from collections import Counter
from collections.abc import Hashable, Sequence
from typing import Any

from evalkit.stats import wilson_interval

KAPPA_MIN = 0.6
ACCURACY_MIN = 0.80
APPROVE_MIN = 4  # nota geral do juiz >= 4 = aprovado
TIE = "tie"


def approved(overall: int | float | None) -> bool:
    return overall is not None and overall >= APPROVE_MIN


def meets_criterion(kappa: float | None, accuracy: float | None) -> bool:
    """Critério de decisão. Kappa indefinido (None) não prova concordância: reprova."""
    return kappa is not None and accuracy is not None and kappa >= KAPPA_MIN and accuracy >= ACCURACY_MIN


# --------------------------------------------------------------------------- concordância
def cohen_kappa(a: Sequence[Hashable], b: Sequence[Hashable]) -> float | None:
    """Kappa de Cohen entre dois avaliadores (categorias quaisquer). None sem dados ou quando a concordância ao
    acaso é 1 (os dois só usam uma categoria: o kappa é indefinido)."""
    if len(a) != len(b):
        raise ValueError("as duas sequências devem ter o mesmo tamanho")
    n = len(a)
    if n == 0:
        return None
    po = sum(x == y for x, y in zip(a, b, strict=True)) / n
    ca, cb = Counter(a), Counter(b)
    pe = sum(ca[k] * cb[k] for k in ca.keys() | cb.keys()) / (n * n)
    if math.isclose(pe, 1.0):
        return None
    return (po - pe) / (1 - pe)


def bootstrap_kappa_ci(
    a: Sequence[Hashable], b: Sequence[Hashable], *, seed: int = 20260, n_boot: int = 2000, alpha: float = 0.05
) -> tuple[float, float] | None:
    """IC bootstrap percentil do kappa (reamostra os pares). Reamostras com kappa indefinido são descartadas; se
    mais da metade for descartada, devolve None."""
    n = len(a)
    if n == 0:
        return None
    rng = random.Random(seed)
    values = []
    for _ in range(n_boot):
        idx = [rng.randrange(n) for _ in range(n)]
        k = cohen_kappa([a[i] for i in idx], [b[i] for i in idx])
        if k is not None:
            values.append(k)
    if len(values) < n_boot / 2:
        return None
    values.sort()
    return values[int((alpha / 2) * len(values))], values[min(len(values) - 1, int((1 - alpha / 2) * len(values)))]


def binary_metrics(judge: Sequence[bool], truth: Sequence[bool]) -> dict[str, Any]:
    """Juiz (aprova/reprova) contra o scorer (passed). Positivo = aprovação. Precisão e revocação são None quando
    o denominador é zero."""
    if len(judge) != len(truth):
        raise ValueError("as duas sequências devem ter o mesmo tamanho")
    tp = sum(j and t for j, t in zip(judge, truth, strict=True))
    fp = sum(j and not t for j, t in zip(judge, truth, strict=True))
    fn = sum(not j and t for j, t in zip(judge, truth, strict=True))
    tn = sum(not j and not t for j, t in zip(judge, truth, strict=True))
    n = len(judge)
    return {
        "n": n, "tp": tp, "fp": fp, "fn": fn, "tn": tn,
        "accuracy": (tp + tn) / n if n else None,
        "precision": tp / (tp + fp) if tp + fp else None,
        "recall": tp / (tp + fn) if tp + fn else None,
        "kappa": cohen_kappa(list(judge), list(truth)),
    }


def accuracy_ci(correct: int, n: int) -> tuple[float, float] | None:
    return wilson_interval(correct, n)


def _ranks(values: Sequence[float]) -> list[float]:
    order = sorted(range(len(values)), key=lambda i: values[i])
    ranks = [0.0] * len(values)
    i = 0
    while i < len(order):
        j = i
        while j + 1 < len(order) and values[order[j + 1]] == values[order[i]]:
            j += 1
        for k in range(i, j + 1):
            ranks[order[k]] = (i + j) / 2 + 1  # posto médio dos empates
        i = j + 1
    return ranks


def spearman(x: Sequence[float], y: Sequence[float]) -> float | None:
    """Correlação de Spearman (Pearson sobre os postos, com posto médio nos empates). None com menos de 3 pares ou
    quando um dos lados é constante."""
    if len(x) != len(y):
        raise ValueError("as duas sequências devem ter o mesmo tamanho")
    n = len(x)
    if n < 3:
        return None
    rx, ry = _ranks(x), _ranks(y)
    mx, my = sum(rx) / n, sum(ry) / n
    sxx = sum((a - mx) ** 2 for a in rx)
    syy = sum((b - my) ** 2 for b in ry)
    if sxx == 0 or syy == 0:
        return None
    return sum((a - mx) * (b - my) for a, b in zip(rx, ry, strict=True)) / math.sqrt(sxx * syy)


# --------------------------------------------------------------------------- pareado
def pairwise_outcome(first: str | None, second: str | None) -> dict[str, Any]:
    """Une as duas rodadas (ordens trocadas) de um prompt. `first`/`second` = quem ganhou cada rodada (nome do
    provedor ou "tie"; None = rodada com `judge_error`).

    - Concordam: vale a preferência.
    - Cada rodada escolheu um modelo diferente (= o juiz escolheu a MESMA posição nas duas): `flipped`, o juiz
      mudou de lado só por causa da ordem; conta como empate.
    - Uma rodada deu empate e a outra escolheu um lado: inconsistente (`tie_vs_pick`); conta como empate.
    - Alguma rodada falhou: `valid` False (o prompt sai do pareado)."""
    if first is None or second is None:
        return {"valid": False, "final": None, "flipped": False, "tie_vs_pick": False}
    if first == second:
        return {"valid": True, "final": first, "flipped": False, "tie_vs_pick": False}
    flipped = first != TIE and second != TIE
    return {"valid": True, "final": TIE, "flipped": flipped, "tie_vs_pick": not flipped}


def position_bias(outcomes: Sequence[dict[str, Any]]) -> dict[str, Any]:
    """Índice de viés de posição = fração dos prompts válidos em que o juiz trocou de lado só por causa da ordem.
    `order_inconsistency` soma também as rodadas em que uma deu empate e a outra escolheu um lado."""
    valid = [o for o in outcomes if o["valid"]]
    n = len(valid)
    flips = sum(o["flipped"] for o in valid)
    mixed = sum(o["tie_vs_pick"] for o in valid)
    return {
        "prompts": n, "flipped": flips, "tie_vs_pick": mixed,
        "position_bias_index": flips / n if n else None,
        "order_inconsistency": (flips + mixed) / n if n else None,
    }


def win_rate(finals: Sequence[str], model: str) -> float | None:
    """Fração de vitórias de `model` nas preferências finais, com empate valendo 0,5."""
    if not finals:
        return None
    return sum(1.0 if f == model else 0.5 if f == TIE else 0.0 for f in finals) / len(finals)


def mean(values: Sequence[float]) -> float | None:
    return sum(values) / len(values) if values else None


def gap(a: float | None, b: float | None) -> float | None:
    return None if a is None or b is None else a - b
