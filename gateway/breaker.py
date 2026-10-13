"""Circuit breaker por provedor: closed -> open -> half_open -> closed.

- `closed`: tudo passa; `failure_threshold` falhas CONSECUTIVAS abrem (um sucesso zera).
- `open`: nada passa até `cooldown_s`; depois vira `half_open`.
- `half_open`: deixa passar no máximo `half_open_max_calls` sondas concorrentes. Sucesso
  fecha; falha reabre e reinicia o cooldown.

Cada admissão carrega a "geração" do estado em que nasceu. Resultado de uma chamada que
começou antes de uma transição (por exemplo, uma chamada lenta iniciada em `closed` que
termina com o breaker já em `half_open`) é ignorado, para não ser confundido com uma sonda.
Todas as operações usam um `asyncio.Lock`. Relógio injetável.
"""

import asyncio
import time
from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum
from typing import Any

Clock = Callable[[], float]


class BreakerState(str, Enum):
    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


@dataclass(frozen=True)
class Admission:
    allowed: bool
    retry_after_s: float | None = None  # só quando negada por `open`
    probe: bool = False
    generation: int = 0


class CircuitBreaker:
    def __init__(
        self,
        failure_threshold: int = 3,
        cooldown_s: float = 30.0,
        half_open_max_calls: int = 1,
        now: Clock = time.monotonic,
    ) -> None:
        self.failure_threshold = failure_threshold
        self.cooldown_s = cooldown_s
        self.half_open_max_calls = half_open_max_calls
        self._now = now
        self._lock = asyncio.Lock()
        self._state = BreakerState.CLOSED
        self._failures = 0
        self._opened_at = 0.0
        self._probes = 0
        self._gen = 0

    # --- transições (chamar com o lock, exceto em snapshot, que só lê/avança por tempo) ---
    def _transition(self, state: BreakerState) -> None:
        self._state = state
        self._gen += 1
        self._failures = 0
        self._probes = 0
        if state is BreakerState.OPEN:
            self._opened_at = self._now()

    def _tick(self) -> None:
        if self._state is BreakerState.OPEN and self._now() - self._opened_at >= self.cooldown_s:
            self._transition(BreakerState.HALF_OPEN)

    # --- API ---
    async def acquire(self) -> Admission:
        async with self._lock:
            self._tick()
            if self._state is BreakerState.CLOSED:
                return Admission(True, generation=self._gen)
            if self._state is BreakerState.OPEN:
                remaining = self._opened_at + self.cooldown_s - self._now()
                return Admission(False, retry_after_s=max(0.0, remaining))
            if self._probes < self.half_open_max_calls:
                self._probes += 1
                return Admission(True, probe=True, generation=self._gen)
            return Admission(False)  # sondas ocupadas: sem estimativa de espera

    async def record_success(self, adm: Admission) -> None:
        async with self._lock:
            if adm.generation != self._gen:
                return  # resultado de antes de uma transição
            if adm.probe:
                self._transition(BreakerState.CLOSED)
            elif self._state is BreakerState.CLOSED:
                self._failures = 0

    async def record_failure(self, adm: Admission) -> None:
        async with self._lock:
            if adm.generation != self._gen:
                return
            if adm.probe:
                self._transition(BreakerState.OPEN)
            elif self._state is BreakerState.CLOSED:
                self._failures += 1
                if self._failures >= self.failure_threshold:
                    self._transition(BreakerState.OPEN)

    async def release(self, adm: Admission) -> None:
        """Devolve a vaga de sonda quando a chamada não terminou em sucesso nem em falha
        contável (429, erro do cliente, cancelamento, ou pulada depois da admissão)."""
        async with self._lock:
            if adm.probe and adm.generation == self._gen:
                self._probes = max(0, self._probes - 1)

    def snapshot(self) -> dict[str, Any]:
        self._tick()
        remaining = None
        if self._state is BreakerState.OPEN:
            remaining = max(0.0, self._opened_at + self.cooldown_s - self._now())
        return {
            "state": self._state.value,
            "consecutive_failures": self._failures,
            "open_remaining_s": None if remaining is None else round(remaining, 2),
        }
