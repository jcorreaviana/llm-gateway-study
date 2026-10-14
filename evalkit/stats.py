"""Estatística da avaliação, sem dependências (só a biblioteca padrão).

- `wilson_interval`: intervalo de Wilson para uma proporção (aprovação).
- `percentile`: interpolação linear (p50, p95).
- `bootstrap_mean_ci`: intervalo por bootstrap percentil da média, com semente fixa (reprodutível).
- `mcnemar_exact`: teste exato (binomial bilateral) para aprovações pareadas.
"""

from __future__ import annotations

import math
import random
from collections.abc import Sequence

Z95 = 1.959963984540054


def wilson_interval(successes: int, n: int, z: float = Z95) -> tuple[float, float] | None:
    """Intervalo de Wilson (padrão 95%). None quando n == 0."""
    if n <= 0:
        return None
    if not 0 <= successes <= n:
        raise ValueError("successes deve estar entre 0 e n")
    p = successes / n
    denom = 1 + z * z / n
    center = (p + z * z / (2 * n)) / denom
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / denom
    return max(0.0, center - half), min(1.0, center + half)


def percentile(values: Sequence[float], q: float) -> float | None:
    """Percentil `q` (0..100) com interpolação linear entre os vizinhos (método padrão do numpy)."""
    if not values:
        return None
    ordered = sorted(values)
    pos = (len(ordered) - 1) * q / 100
    lo, hi = math.floor(pos), math.ceil(pos)
    return ordered[lo] + (ordered[hi] - ordered[lo]) * (pos - lo)


def bootstrap_mean_ci(
    values: Sequence[float], *, seed: int = 20260, n_boot: int = 10000, alpha: float = 0.05
) -> tuple[float, float, float] | None:
    """(média, limite inferior, limite superior) do bootstrap percentil. None sem dados."""
    if not values:
        return None
    rng = random.Random(seed)
    n = len(values)
    means = sorted(sum(values[rng.randrange(n)] for _ in range(n)) / n for _ in range(n_boot))
    lo = means[int((alpha / 2) * n_boot)]
    hi = means[min(n_boot - 1, int((1 - alpha / 2) * n_boot))]
    return sum(values) / n, lo, hi


def _binom_cdf(k: int, n: int) -> float:
    return sum(math.comb(n, i) for i in range(k + 1)) / 2**n


def mcnemar_exact(only_a: int, only_b: int) -> float:
    """p-valor bilateral exato do McNemar: `only_a` = A aprovou e B não; `only_b` = o inverso. Sob H0 os
    discordantes seguem Binomial(n, 0.5)."""
    n = only_a + only_b
    if n == 0:
        return 1.0
    return min(1.0, 2 * _binom_cdf(min(only_a, only_b), n))
