"""Métricas de uma cena, calculadas a partir das requisições registradas pelo runner.

Cada requisição é um dict com (no mínimo) `t_s` (instante de envio, em segundos desde o início da cena),
`provider` (quem respondeu, "none" se ninguém) e `reached_mock` (a chamada chegou ao mock?). Nada aqui lê
texto de resposta.
"""

from __future__ import annotations

import statistics
from collections.abc import Iterable, Mapping, Sequence
from typing import Any

# Diferença máxima (s) entre repetições para o tempo até voltar ao mock ser tratado como "o mesmo": a
# resolução da medida é o intervalo de sondagem do runner (0,25 a 0,5 s), então 1 s são 2 a 4 intervalos.
TIME_TOLERANCE_S = 1.0
MOCK_PROVIDER = "mock_market"


def rle(items: Iterable[Any]) -> str:
    """Sequência comprimida: ['a','a','b'] -> 'a x2|b'. Vazia -> '-'."""
    out: list[str] = []
    prev: Any = None
    count = 0
    for item in items:
        if count and item == prev:
            count += 1
            continue
        if count:
            out.append(_run(prev, count))
        prev, count = item, 1
    if count:
        out.append(_run(prev, count))
    return "|".join(out) or "-"


def _run(item: Any, count: int) -> str:
    return str(item) if count == 1 else f"{item} x{count}"


def calls_until_stop(cumulative: Sequence[int], tail: int = 3) -> int | None:
    """Quantas chamadas o mock recebeu até o gateway parar de chamá-lo.

    `cumulative[i]` = total recebido pelo mock depois da requisição i. Devolve o total no último aumento,
    desde que o contador tenha ficado parado nas últimas `tail` requisições (ou mais); se ainda crescia
    perto do fim (ou nunca cresceu) devolve None: o gateway não parou dentro da amostra.
    """
    if len(cumulative) < tail + 1:
        return None
    last_increase = None
    prev = 0
    for i, value in enumerate(cumulative):
        if value > prev:
            last_increase = i
        prev = value
    if last_increase is None:
        return None
    if len(cumulative) - 1 - last_increase < tail:
        return None
    return cumulative[last_increase]


def first_skipped_index(requests: Sequence[Mapping[str, Any]]) -> int | None:
    """Posição (1-based) da primeira requisição que NÃO chegou ao mock, depois de pelo menos uma que chegou."""
    seen = False
    for i, req in enumerate(requests, start=1):
        if req.get("reached_mock"):
            seen = True
        elif seen:
            return i
    return None


def time_to_return_s(requests: Sequence[Mapping[str, Any]], t0_s: float) -> float | None:
    """Tempo (s) de `t0_s` até o envio da primeira requisição que chegou ao mock; None se nenhuma chegou.
    A resolução é o intervalo de sondagem do runner."""
    # 1 ms de folga: o `t_s` das requisições é arredondado a 3 casas e o `t0_s` pode não ser
    times = [r["t_s"] for r in requests if r.get("reached_mock") and r["t_s"] >= t0_s - 0.001]
    return round(max(0.0, min(times) - t0_s), 2) if times else None


def window_counts(requests: Sequence[Mapping[str, Any]], width_s: float) -> tuple[int, int] | None:
    """(respondidas pelo mock, respondidas por outro) entre as requisições enviadas na janela de `width_s`
    que começa na primeira que o mock respondeu. None se o mock nunca respondeu."""
    answered = [r["t_s"] for r in requests if r.get("provider") == MOCK_PROVIDER]
    if not answered:
        return None
    start = min(answered)
    inside = [r for r in requests if start <= r["t_s"] < start + width_s]
    mock = sum(1 for r in inside if r.get("provider") == MOCK_PROVIDER)
    return mock, len(inside) - mock


def attempts_total(requests: Sequence[Mapping[str, Any]]) -> int:
    """Soma das tentativas de fallback (None = o gateway não informou = 0)."""
    return sum(int(r.get("attempts") or 0) for r in requests)


def signature(run: Mapping[str, Any]) -> tuple:
    """O que precisa coincidir entre repetições para a cena ser 'estável' (tempos ficam de fora: têm
    tolerância própria, ver `time_stable`)."""
    reqs = run["requests"]
    return (
        tuple(r["status"] for r in reqs),
        tuple(r["provider"] for r in reqs),
        tuple(int(r.get("attempts") or 0) for r in reqs),
        run["mock"]["received"],
    )


def diverges(runs: Sequence[Mapping[str, Any]]) -> bool:
    return len({signature(r) for r in runs}) > 1


def time_summary(values: Iterable[float | None]) -> dict[str, float | None]:
    known = [v for v in values if v is not None]
    if not known:
        return {"min": None, "median": None, "max": None, "spread": None}
    return {"min": min(known), "median": round(statistics.median(known), 2), "max": max(known),
            "spread": round(max(known) - min(known), 2)}


def time_stable(values: Sequence[float | None]) -> bool:
    """Todas as repetições mediram (ou todas deixaram de medir) e a dispersão cabe na tolerância."""
    known = [v for v in values if v is not None]
    if len(known) not in (0, len(values)):
        return False
    return not known or max(known) - min(known) <= TIME_TOLERANCE_S
