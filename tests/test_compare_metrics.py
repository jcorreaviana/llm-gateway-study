"""Métricas do comparativo (Marco 14) com valores conhecidos: nada de rede, processos ou litellm."""

import pytest

from comparekit import metrics


def req(t, provider="mock_market", reached=True, status=200, attempts=0):
    return {"t_s": t, "provider": provider, "reached_mock": reached, "status": status, "attempts": attempts}


def test_rle_compresses_runs_and_keeps_order():
    assert metrics.rle(["a", "a", "b", "a"]) == "a x2|b|a"
    assert metrics.rle([200, 200, 200]) == "200 x3"
    assert metrics.rle([]) == "-"


@pytest.mark.parametrize(
    ("cumulative", "expected"),
    [
        ([1, 2, 3, 3, 3, 3, 3], 3),          # parou no 3 e ficou parado 4 requisições
        ([1, 2, 3, 3, 3], None),             # só 2 requisições paradas no fim: ainda não dá para dizer
        ([1, 2, 3, 4, 5, 6, 7, 8], None),    # nunca parou
        ([0, 0, 0, 0, 0], None),             # nunca chegou ao mock
        ([1, 1, 1, 1], 1),                   # chegou uma vez e depois parou
        ([1, 2], None),                      # amostra curta demais
    ],
)
def test_calls_until_stop(cumulative, expected):
    assert metrics.calls_until_stop(cumulative) == expected


def test_calls_until_stop_respects_custom_tail():
    assert metrics.calls_until_stop([1, 2, 3, 3, 3], tail=2) == 3


def test_first_skipped_index_needs_a_call_first():
    assert metrics.first_skipped_index([req(0), req(1), req(2, reached=False), req(3)]) == 3
    assert metrics.first_skipped_index([req(0, reached=False), req(1)]) is None  # pular antes de chamar não é "parar"
    assert metrics.first_skipped_index([req(0), req(1)]) is None


def test_time_to_return_uses_first_call_at_or_after_t0():
    reqs = [req(0.0), req(1.0, reached=False), req(1.5, reached=False), req(2.5), req(3.0)]
    assert metrics.time_to_return_s(reqs, t0_s=0.4) == 2.1       # 2.5 - 0.4
    assert metrics.time_to_return_s([req(0.0), req(1.0, reached=False)], t0_s=0.5) is None


def test_time_to_return_tolerates_rounding_of_t0():
    assert metrics.time_to_return_s([req(5.0)], t0_s=5.0004) == 0.0


def test_window_counts_splits_mock_from_fallback():
    reqs = [req(0.0, provider="ollama_qwen", reached=False), req(1.0), req(1.25, provider="ollama_qwen"),
            req(1.5, provider="ollama_qwen"), req(3.4), req(3.5, provider="mock_market")]
    # janela de 2 s a partir da primeira resposta do mock (t=1.0): [1.0, 3.0)
    assert metrics.window_counts(reqs, 2.0) == (1, 2)
    assert metrics.window_counts([req(0, provider="ollama_qwen")], 2.0) is None


def test_attempts_total_treats_missing_as_zero():
    assert metrics.attempts_total([req(0, attempts=1), req(1, attempts=None), req(2, attempts=2)]) == 3


def run(statuses, providers, received, attempts=None):
    attempts = attempts or [0] * len(statuses)
    steps = zip(statuses, providers, attempts, strict=True)
    return {"requests": [{"status": s, "provider": p, "attempts": a} for s, p, a in steps],
            "mock": {"received": received}}


def test_divergence_between_repetitions():
    same = [run([200, 200], ["m", "q"], 1) for _ in range(3)]
    assert not metrics.diverges(same)
    different = [*same[:2], run([200, 200], ["m", "m"], 2)]
    assert metrics.diverges(different)
    assert metrics.diverges([run([200], ["m"], 1), run([200], ["m"], 1, attempts=[1])])  # tentativas também contam


def test_time_summary_and_stability():
    assert metrics.time_summary([2.0, 2.4, 3.0]) == {"min": 2.0, "median": 2.4, "max": 3.0, "spread": 1.0}
    assert metrics.time_summary([None, None])["min"] is None
    assert metrics.time_stable([2.0, 2.5, 2.9])
    assert not metrics.time_stable([2.0, 3.5])           # dispersão de 1,5 s > tolerância de 1 s
    assert not metrics.time_stable([2.0, None, 2.1])     # uma repetição não mediu
    assert metrics.time_stable([None, None, None])
