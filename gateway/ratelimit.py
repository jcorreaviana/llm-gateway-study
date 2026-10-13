"""Token bucket local por provedor (RPM e TPM), com reabastecimento contínuo.

O nível sobe à razão `capacidade / 60` por segundo (não por janela fixa) até a capacidade.
O relógio é injetável (`now`) para testar sem `sleep`. Sem `await` aqui: no asyncio de uma
thread, cada operação é atômica em relação às outras tarefas.
"""

import time
from collections.abc import Callable
from typing import Any

Clock = Callable[[], float]


class TokenBucket:
    def __init__(self, per_minute: int, now: Clock = time.monotonic) -> None:
        if per_minute <= 0:
            raise ValueError("per_minute deve ser > 0")
        self.capacity = float(per_minute)
        self.rate = per_minute / 60.0  # unidades por segundo
        self._now = now
        self._level = self.capacity  # começa cheio
        self._stamp = now()

    def _refill(self) -> None:
        t = self._now()
        elapsed = max(0.0, t - self._stamp)
        self._stamp = t
        self._level = min(self.capacity, self._level + elapsed * self.rate)

    @property
    def level(self) -> float:
        self._refill()
        return self._level

    def _cost(self, amount: float) -> float:
        # Um pedido maior que a capacidade nunca caberia; limita ao balde cheio para que
        # ele possa passar uma vez (e deixe o balde zerado) em vez de ficar bloqueado para sempre.
        return min(float(amount), self.capacity)

    def wait_time(self, amount: float) -> float:
        """Segundos até haver capacidade para `amount` (0 se já há)."""
        deficit = self._cost(amount) - self.level
        return 0.0 if deficit <= 0 else deficit / self.rate

    def try_consume(self, amount: float) -> bool:
        cost = self._cost(amount)
        if self.level < cost:
            return False
        self._level -= cost
        return True

    def adjust(self, delta: float) -> None:
        """Consome `delta` a mais (positivo) ou devolve (negativo). Pode deixar o nível
        negativo (dívida), que o reabastecimento vai quitando."""
        self._refill()
        self._level = min(self.capacity, self._level - delta)


class ProviderLimiter:
    """RPM e TPM independentes. Qualquer um pode ser `None` (sem limite)."""

    def __init__(self, rpm: int | None, tpm: int | None, now: Clock = time.monotonic) -> None:
        self.rpm = TokenBucket(rpm, now) if rpm else None
        self.tpm = TokenBucket(tpm, now) if tpm else None

    def try_acquire(self, est_tokens: int) -> float | None:
        """None se reservou 1 requisição e `est_tokens`; senão, segundos até haver capacidade.
        Só consome se os dois baldes tiverem espaço (não gasta um e nega o outro)."""
        waits = []
        if self.rpm:
            waits.append(self.rpm.wait_time(1))
        if self.tpm:
            waits.append(self.tpm.wait_time(est_tokens))
        wait = max(waits, default=0.0)
        if wait > 0:
            return wait
        if self.rpm:
            self.rpm.try_consume(1)
        if self.tpm:
            self.tpm.try_consume(est_tokens)
        return None

    def settle(self, est_tokens: int, usage: Any) -> None:
        """Em sucesso, troca a estimativa pelo `usage.total_tokens` real, se vier."""
        total = usage.get("total_tokens") if isinstance(usage, dict) else None
        if self.tpm and isinstance(total, int) and not isinstance(total, bool) and total >= 0:
            self.tpm.adjust(total - est_tokens)

    def snapshot(self) -> dict[str, Any]:
        def one(b: TokenBucket | None) -> dict[str, float] | None:
            return None if b is None else {"level": round(b.level, 2), "capacity": b.capacity}

        return {"rpm": one(self.rpm), "tpm": one(self.tpm)}
