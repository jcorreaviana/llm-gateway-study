"""Disponibilidade por provedor: cooldown (Retry-After) -> circuit breaker -> token bucket.

`ProviderGuard.check` decide ANTES da chamada; `settle` registra o resultado depois.
"""

import time
from dataclasses import dataclass
from typing import Any

from gateway.breaker import Admission, CircuitBreaker
from gateway.config import ProviderConfig
from gateway.providers import FailureReason, ProviderResult, ProviderSuccess
from gateway.ratelimit import Clock, ProviderLimiter

# Falhas que indicam provedor doente e contam para o breaker. Fora daqui (não contam):
# rate_limited (vai para cooldown), client_error, real_api_blocked e os motivos do router.
BREAKER_FAILURES = frozenset(
    {
        FailureReason.SERVER_ERROR,
        FailureReason.TIMEOUT,
        FailureReason.CONNECTION_ERROR,
        FailureReason.INVALID_RESPONSE,
        FailureReason.AUTH_ERROR,
    }
)


@dataclass(frozen=True)
class Availability:
    reason: FailureReason | None  # None = pode tentar
    retry_after_s: float | None = None  # quando o motivo informa uma espera
    admission: Admission | None = None


class ProviderGuard:
    def __init__(self, cfg: ProviderConfig, now: Clock = time.monotonic) -> None:
        self._now = now
        self.default_retry_after_s = cfg.default_retry_after_s
        self.breaker = CircuitBreaker(
            cfg.breaker.failure_threshold,
            cfg.breaker.cooldown_s,
            cfg.breaker.half_open_max_calls,
            now,
        )
        rl = cfg.rate_limit
        self.limiter = ProviderLimiter(rl.rpm, rl.tpm, now) if rl and (rl.rpm or rl.tpm) else None
        self._cooldown_until = 0.0

    def cooldown_remaining(self) -> float:
        return max(0.0, self._cooldown_until - self._now())

    async def check(self, est_tokens: int) -> Availability:
        remaining = self.cooldown_remaining()
        if remaining > 0:
            return Availability(FailureReason.COOLING_DOWN, remaining)
        adm = await self.breaker.acquire()
        if not adm.allowed:
            return Availability(FailureReason.BREAKER_OPEN, adm.retry_after_s)
        if self.limiter:
            wait = self.limiter.try_acquire(est_tokens)
            if wait is not None:
                await self.breaker.release(adm)  # a sonda não chegou a ser feita
                return Availability(FailureReason.RATE_LIMIT_LOCAL, wait)
        return Availability(None, admission=adm)

    async def settle(self, adm: Admission, est_tokens: int, result: ProviderResult) -> float | None:
        """Registra o resultado. Devolve o cooldown (s) se foi um 429, senão None."""
        if isinstance(result, ProviderSuccess):
            await self.breaker.record_success(adm)
            if self.limiter:
                self.limiter.settle(est_tokens, result.data.get("usage"))
            return None
        if result.reason in BREAKER_FAILURES:
            await self.breaker.record_failure(adm)
            return None
        await self.breaker.release(adm)
        if result.reason is FailureReason.RATE_LIMITED:
            wait = result.retry_after_s
            if wait is None:
                wait = self.default_retry_after_s
            self._cooldown_until = self._now() + wait
            return wait
        return None

    def snapshot(self) -> dict[str, Any]:
        return {
            "breaker": self.breaker.snapshot(),
            "buckets": self.limiter.snapshot() if self.limiter else {"rpm": None, "tpm": None},
            "cooldown_remaining_s": round(self.cooldown_remaining(), 2),
        }
