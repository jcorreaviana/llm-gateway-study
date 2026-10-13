from gateway.breaker import BreakerState, CircuitBreaker


class Clock:
    def __init__(self) -> None:
        self.t = 0.0

    def __call__(self) -> float:
        return self.t


def make(threshold=3, cooldown=30.0, probes=1):
    clock = Clock()
    return CircuitBreaker(threshold, cooldown, probes, clock), clock


async def fail(b, n=1):
    for _ in range(n):
        adm = await b.acquire()
        assert adm.allowed
        await b.record_failure(adm)


async def open_it(b):
    await fail(b, b.failure_threshold)
    assert b.snapshot()["state"] == "open"


async def test_opens_after_consecutive_failures():
    b, _ = make()
    await fail(b, 2)
    assert b.snapshot()["state"] == "closed"
    await fail(b, 1)
    assert b.snapshot()["state"] == "open"
    adm = await b.acquire()
    assert not adm.allowed and adm.retry_after_s == 30.0


async def test_success_resets_failure_counter():
    b, _ = make()
    await fail(b, 2)
    await b.record_success(await b.acquire())
    await fail(b, 2)  # 2 depois do sucesso, não 4
    assert b.snapshot()["state"] == "closed"
    await fail(b, 1)
    assert b.snapshot()["state"] == "open"


async def test_open_becomes_half_open_after_cooldown():
    b, clock = make(cooldown=30)
    await open_it(b)
    clock.t = 29.9
    assert not (await b.acquire()).allowed
    clock.t = 30.0
    assert b.snapshot()["state"] == "half_open"
    adm = await b.acquire()
    assert adm.allowed and adm.probe


async def test_successful_probe_closes():
    b, clock = make()
    await open_it(b)
    clock.t = 30
    await b.record_success(await b.acquire())
    assert b.snapshot()["state"] == BreakerState.CLOSED.value
    assert (await b.acquire()).allowed


async def test_failed_probe_reopens_and_restarts_cooldown():
    b, clock = make(cooldown=30)
    await open_it(b)
    clock.t = 40
    await b.record_failure(await b.acquire())
    snap = b.snapshot()
    assert snap["state"] == "open" and snap["open_remaining_s"] == 30
    clock.t = 69
    assert not (await b.acquire()).allowed  # cooldown recomeçou em t=40
    clock.t = 70
    assert (await b.acquire()).probe


async def test_half_open_limits_concurrent_probes():
    b, clock = make(probes=2)
    await open_it(b)
    clock.t = 30
    first, second, third = await b.acquire(), await b.acquire(), await b.acquire()
    assert first.allowed and second.allowed and not third.allowed
    await b.release(first)  # sonda terminou sem veredito (ex.: 429): vaga volta
    assert (await b.acquire()).allowed


async def test_stale_result_from_before_transition_is_ignored():
    b, clock = make()
    slow = await b.acquire()  # chamada lenta iniciada em closed
    await fail(b, 3)
    clock.t = 30
    probe = await b.acquire()
    await b.record_success(slow)  # não pode fechar nem contar como sonda
    assert b.snapshot()["state"] == "half_open"
    await b.record_success(probe)
    assert b.snapshot()["state"] == "closed"


async def test_failures_while_open_do_not_extend_cooldown():
    b, clock = make()
    straggler = await b.acquire()
    await open_it(b)
    clock.t = 10
    await b.record_failure(straggler)
    assert b.snapshot()["open_remaining_s"] == 20
