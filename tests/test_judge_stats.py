"""Estatística da calibração do juiz com valores conhecidos (Marco 11). Sem rede, sem Ollama."""

import math

import pytest

from evalkit import judge_stats as js

T, F = True, False


def pairs(counts: dict[tuple, int]) -> tuple[list, list]:
    a, b = [], []
    for (x, y), n in counts.items():
        a += [x] * n
        b += [y] * n
    return a, b


# --------------------------------------------------------------------------- kappa de Cohen
def test_kappa_known_two_by_two():
    a, b = pairs({(T, T): 20, (T, F): 5, (F, T): 10, (F, F): 15})  # po=0,7  pe=0,5
    assert js.cohen_kappa(a, b) == pytest.approx(0.4)


def test_kappa_known_three_categories():
    a, b = list("xxyyzz"), list("xyyyzx")  # po=4/6  pe=12/36
    assert js.cohen_kappa(a, b) == pytest.approx(0.5)


def test_kappa_extremes():
    assert js.cohen_kappa([T, F, T, F], [T, F, T, F]) == pytest.approx(1.0)
    assert js.cohen_kappa([T, F, T, F], [F, T, F, T]) == pytest.approx(-1.0)
    a, b = pairs({(T, T): 5, (T, F): 5, (F, T): 5, (F, F): 5})
    assert js.cohen_kappa(a, b) == pytest.approx(0.0)


def test_kappa_undefined_cases_are_none():
    assert js.cohen_kappa([], []) is None
    assert js.cohen_kappa([T, T, T], [T, T, T]) is None  # só uma categoria dos dois lados: pe = 1
    with pytest.raises(ValueError):
        js.cohen_kappa([T], [T, F])


def test_approving_everything_has_high_accuracy_but_zero_kappa():
    truth = [T] * 18 + [F] * 2
    m = js.binary_metrics([T] * 20, truth)
    assert m["accuracy"] == pytest.approx(0.9) and m["recall"] == 1.0 and m["kappa"] == pytest.approx(0.0)
    assert not js.meets_criterion(m["kappa"], m["accuracy"])  # o critério exige os DOIS


def test_bootstrap_kappa_ci_is_reproducible_and_brackets_strong_agreement():
    a, b = pairs({(T, T): 30, (F, F): 25, (T, F): 3, (F, T): 2})
    ci = js.bootstrap_kappa_ci(a, b, seed=7)
    assert ci == js.bootstrap_kappa_ci(a, b, seed=7)
    point = js.cohen_kappa(a, b)
    assert ci[0] < point < ci[1] and ci[0] > 0.5 and ci[1] <= 1.0


def test_bootstrap_kappa_ci_none_when_undefined_or_empty():
    assert js.bootstrap_kappa_ci([], []) is None
    assert js.bootstrap_kappa_ci([T] * 10, [T] * 10) is None


# --------------------------------------------------------------------------- acurácia, precisão, revocação
def test_binary_metrics_known_values():
    m = js.binary_metrics([T, T, T, F, F], [T, T, F, T, F])
    assert (m["tp"], m["fp"], m["fn"], m["tn"]) == (2, 1, 1, 1)
    assert m["accuracy"] == pytest.approx(0.6)
    assert m["precision"] == pytest.approx(2 / 3) and m["recall"] == pytest.approx(2 / 3)
    assert m["kappa"] == pytest.approx((0.6 - 0.52) / 0.48)


def test_binary_metrics_zero_denominators_are_none():
    m = js.binary_metrics([F, F, F], [T, F, F])  # o juiz nunca aprova
    assert m["precision"] is None and m["recall"] == 0.0 and m["accuracy"] == pytest.approx(2 / 3)
    empty = js.binary_metrics([], [])
    assert empty["n"] == 0 and empty["accuracy"] is None and empty["kappa"] is None
    with pytest.raises(ValueError):
        js.binary_metrics([T], [])


def test_approved_threshold_is_four():
    assert js.APPROVE_MIN == 4
    assert js.approved(4) and js.approved(5) and not js.approved(3) and not js.approved(None)


# --------------------------------------------------------------------------- critério de decisão (fixado antes)
def test_decision_criterion_constants_and_boundaries():
    assert (js.KAPPA_MIN, js.ACCURACY_MIN) == (0.6, 0.80)
    assert js.meets_criterion(0.6, 0.80)
    assert not js.meets_criterion(0.5999, 0.95)
    assert not js.meets_criterion(0.95, 0.7999)
    assert not js.meets_criterion(None, 0.99) and not js.meets_criterion(0.9, None)


# --------------------------------------------------------------------------- Spearman
def test_spearman_known_value_with_ties():
    # postos de y: 1, 2, 3,5, 5, 3,5 -> cov 8, var 10 e 9,5 -> 8 / sqrt(95)
    assert js.spearman([1, 2, 3, 4, 5], [5, 6, 7, 8, 7]) == pytest.approx(8 / math.sqrt(95))


def test_spearman_monotone_and_reversed():
    assert js.spearman([1, 2, 3, 4], [10, 20, 30, 400]) == pytest.approx(1.0)
    assert js.spearman([1, 2, 3, 4], [4, 3, 2, 1]) == pytest.approx(-1.0)


def test_spearman_undefined_cases():
    assert js.spearman([1, 2, 3], [4, 4, 4]) is None  # um lado constante
    assert js.spearman([1, 2], [1, 2]) is None  # poucos pares
    with pytest.raises(ValueError):
        js.spearman([1, 2, 3], [1, 2])


# --------------------------------------------------------------------------- pareado: viés de posição
Q, L = "ollama_qwen", "ollama_llama"


@pytest.mark.parametrize("first,second,final,flipped,mixed,valid", [
    (Q, Q, Q, F, F, T), (L, L, L, F, F, T), ("tie", "tie", "tie", F, F, T),
    (Q, L, "tie", T, F, T), (L, Q, "tie", T, F, T),  # escolheu a mesma POSIÇÃO nas duas ordens
    (Q, "tie", "tie", F, T, T), ("tie", L, "tie", F, T, T),
    (None, Q, None, F, F, F), (Q, None, None, F, F, F),
])
def test_pairwise_outcome_truth_table(first, second, final, flipped, mixed, valid):
    got = js.pairwise_outcome(first, second)
    assert (got["final"], got["flipped"], got["tie_vs_pick"], got["valid"]) == (final, flipped, mixed, valid)


def test_position_bias_index_known_value():
    outcomes = [js.pairwise_outcome(*x) for x in [(Q, Q), (Q, L), (L, Q), (Q, "tie"), (None, Q)]]
    got = js.position_bias(outcomes)
    assert got["prompts"] == 4 and got["flipped"] == 2 and got["tie_vs_pick"] == 1
    assert got["position_bias_index"] == pytest.approx(0.5)  # 2 de 4 prompts válidos mudaram de lado
    assert got["order_inconsistency"] == pytest.approx(0.75)


def test_position_bias_without_valid_prompts_is_none():
    got = js.position_bias([js.pairwise_outcome(None, None)])
    assert got["position_bias_index"] is None and got["prompts"] == 0


def test_a_judge_that_always_picks_position_a_has_index_one():
    # rodada 1: A = Q ganha (Q); rodada 2 com ordem trocada: A = L ganha (L) -> mesma posição, lados trocados
    outcomes = [js.pairwise_outcome(Q, L) for _ in range(6)]
    assert js.position_bias(outcomes)["position_bias_index"] == 1.0
    assert all(o["final"] == "tie" for o in outcomes)


def test_win_rate_counts_ties_as_half():
    assert js.win_rate([Q, Q, "tie", L], Q) == pytest.approx(0.625)
    assert js.win_rate([Q, Q, "tie", L], L) == pytest.approx(0.375)
    assert js.win_rate([], Q) is None


def test_gap_and_mean_helpers():
    assert js.gap(0.5, 0.25) == 0.25 and js.gap(None, 1) is None and js.gap(1, None) is None
    assert js.mean([1, 2, 3]) == 2 and js.mean([]) is None
