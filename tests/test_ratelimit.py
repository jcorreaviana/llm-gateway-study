import pytest

from gateway.ratelimit import ProviderLimiter, TokenBucket


class Clock:
    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t

    def advance(self, s: float) -> None:
        self.t += s


def test_bucket_blocks_when_exhausted_and_reports_wait():
    clock = Clock()
    b = TokenBucket(60, clock)  # 1 por segundo, capacidade 60
    assert all(b.try_consume(1) for _ in range(60))
    assert b.try_consume(1) is False
    assert b.wait_time(1) == pytest.approx(1.0)
    assert b.wait_time(30) == pytest.approx(30.0)


def test_bucket_refills_continuously_not_by_window():
    clock = Clock()
    b = TokenBucket(60, clock)
    b.try_consume(60)
    clock.advance(0.5)
    assert b.try_consume(1) is False  # 0,5 token: ainda não
    clock.advance(0.5)
    assert b.try_consume(1) is True  # 1 token
    clock.advance(10_000)
    assert b.level == 60  # nunca passa da capacidade


def test_oversized_request_is_capped_at_capacity():
    b = TokenBucket(100, Clock())
    assert b.try_consume(5_000) is True  # passa uma vez, com o balde cheio
    assert b.level == 0
    assert b.wait_time(5_000) == pytest.approx(60.0)


def test_rpm_and_tpm_are_independent():
    clock = Clock()
    lim = ProviderLimiter(rpm=100, tpm=1000, now=clock)
    assert lim.try_acquire(600) is None
    # TPM (400 restantes) bloqueia, mesmo com RPM sobrando
    wait = lim.try_acquire(500)
    assert wait == pytest.approx(100 / (1000 / 60))
    assert lim.rpm.level == 99  # o pedido negado não gastou RPM

    lim2 = ProviderLimiter(rpm=2, tpm=1_000_000, now=clock)
    assert lim2.try_acquire(1) is None and lim2.try_acquire(1) is None
    assert lim2.try_acquire(1) == pytest.approx(30.0)  # RPM bloqueia, TPM sobrando
    assert lim2.tpm.level == 1_000_000 - 2


def test_none_means_unlimited():
    lim = ProviderLimiter(rpm=None, tpm=None, now=Clock())
    assert all(lim.try_acquire(10**9) is None for _ in range(1000))
    assert lim.snapshot() == {"rpm": None, "tpm": None}


def test_wait_resolves_with_time():
    clock = Clock()
    lim = ProviderLimiter(rpm=2, tpm=None, now=clock)
    lim.try_acquire(1), lim.try_acquire(1)
    wait = lim.try_acquire(1)
    clock.advance(wait)
    assert lim.try_acquire(1) is None


def test_usage_adjusts_consumption_to_real_value():
    lim = ProviderLimiter(rpm=None, tpm=1000, now=Clock())
    lim.try_acquire(100)  # estimativa: 100
    lim.settle(100, {"total_tokens": 300})
    assert lim.tpm.level == pytest.approx(700)  # real foi 300
    lim.try_acquire(100)
    lim.settle(100, {"total_tokens": 20})  # real menor: devolve
    assert lim.tpm.level == pytest.approx(700 - 100 + 80)


@pytest.mark.parametrize("usage", [None, {}, {"total_tokens": "x"}, {"total_tokens": True}, {"total_tokens": -5}, "abc"])
def test_invalid_usage_is_ignored(usage):
    lim = ProviderLimiter(rpm=None, tpm=1000, now=Clock())
    lim.try_acquire(100)
    lim.settle(100, usage)
    assert lim.tpm.level == pytest.approx(900)


def test_invalid_capacity_rejected():
    with pytest.raises(ValueError):
        TokenBucket(0)
