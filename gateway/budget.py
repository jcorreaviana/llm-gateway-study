"""Orçamento rígido e persistente de chamadas reais (fail-closed).

- `reserve()` incrementa o contador ANTES da chamada e nunca devolve a reserva: falha,
  timeout ou erro também contam, então um loop com erro não passa do limite.
- Seguro entre threads e processos: lock de arquivo (`msvcrt` no Windows, `fcntl` no resto) em
  volta de ler-incrementar-gravar, e escrita atômica (arquivo temporário + `os.replace`).
- Fail-closed: arquivo ilegível, corrompido ou com valores inválidos conta como orçamento
  esgotado (com erro claro). O contador nunca é reiniciado em silêncio. Sem reset automático:
  o reset manual é `scripts/reset_budget.py`.
"""

import json
import logging
import os
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger("gateway.budget")

if sys.platform == "win32":
    import msvcrt
else:
    import fcntl

_LOCK_TIMEOUT_S = 10.0


class BudgetError(Exception):
    """Estado do orçamento inválido ou inacessível (tratado como esgotado)."""


@dataclass(frozen=True)
class BudgetStatus:
    used: int
    limit: int
    remaining: int
    error: str | None = None  # preenchido quando o arquivo está inválido (remaining = 0)

    def as_dict(self) -> dict:
        d = {"used": self.used, "limit": self.limit, "remaining": self.remaining}
        if self.error:
            d["error"] = self.error
        return d


class _FileLock:
    """Lock exclusivo entre threads (threading.Lock) e processos (arquivo `.lock`)."""

    def __init__(self, path: Path, tlock: threading.Lock) -> None:
        self._path, self._tlock, self._fh = path, tlock, None

    def __enter__(self):
        if not self._tlock.acquire(timeout=_LOCK_TIMEOUT_S):
            raise TimeoutError("lock do budget ocupado")
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            self._fh = open(self._path, "a+b")
            deadline = time.monotonic() + _LOCK_TIMEOUT_S
            while True:
                try:
                    self._lock_fh()
                    return self
                except OSError:
                    if time.monotonic() > deadline:
                        raise TimeoutError("lock do budget ocupado") from None
                    time.sleep(0.005)
        except BaseException:
            self._close()
            self._tlock.release()
            raise

    def _lock_fh(self) -> None:
        if sys.platform == "win32":
            self._fh.seek(0)
            msvcrt.locking(self._fh.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            fcntl.flock(self._fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)

    def _close(self) -> None:
        if self._fh:
            self._fh.close()  # fechar o handle também solta o lock
            self._fh = None

    def __exit__(self, *exc) -> None:
        try:
            if self._fh and sys.platform == "win32":
                self._fh.seek(0)
                msvcrt.locking(self._fh.fileno(), msvcrt.LK_UNLCK, 1)
        finally:
            self._close()
            self._tlock.release()


class Budget:
    def __init__(self, path: Path | str, limit: int = 30) -> None:
        if limit <= 0:
            raise ValueError("limit do budget deve ser > 0")
        self.path = Path(path)
        self.limit = limit
        self._lock_path = self.path.with_name(self.path.name + ".lock")
        self._tlock = threading.Lock()

    def locked(self) -> _FileLock:
        return _FileLock(self._lock_path, self._tlock)

    # --- leitura/escrita (chamar com o lock) --------------------------------------------
    def _read(self) -> int:
        """Lê `used`. Arquivo ausente = 0 (quem chama decide se cria). Inválido = BudgetError."""
        try:
            raw = self.path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return 0
        except OSError as e:
            raise BudgetError(f"budget ilegível ({type(e).__name__}); tratado como esgotado") from None
        try:
            data = json.loads(raw)
        except ValueError:
            raise BudgetError("budget corrompido (JSON inválido); tratado como esgotado") from None
        used = data.get("used") if isinstance(data, dict) else None
        if isinstance(used, bool) or not isinstance(used, int) or used < 0:
            raise BudgetError("budget com valores inválidos; tratado como esgotado")
        return used

    def write(self, used: int) -> None:
        """Escrita atômica: arquivo temporário no mesmo diretório + os.replace."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_name(f"{self.path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump({"used": used}, f)
                f.flush()
                os.fsync(f.fileno())
            for attempt in range(20):  # no Windows o replace pode falhar se alguém estiver lendo
                try:
                    os.replace(tmp, self.path)
                    return
                except PermissionError:
                    if attempt == 19:
                        raise
                    time.sleep(0.01)
        except BaseException:
            tmp.unlink(missing_ok=True)  # não deixa lixo; o arquivo original fica intacto
            raise

    # --- API ---------------------------------------------------------------------------
    def ensure(self) -> None:
        """Cria o arquivo em zero se não existir. Não toca em arquivo existente (nem inválido)."""
        with self.locked():
            if not self.path.exists():
                self.write(0)

    def reserve(self) -> bool:
        """True = reservou uma chamada (contador já incrementado). False = esgotado/inválido."""
        try:
            with self.locked():
                used = self._read()
                if used >= self.limit:
                    return False
                self.write(used + 1)
                return True
        except (BudgetError, OSError, TimeoutError) as e:
            log.error("budget indisponível, tratado como esgotado: %s", e)
            return False

    def status(self) -> BudgetStatus:
        """Somente leitura (não cria o arquivo; ausente = 0 usado)."""
        try:
            with self.locked():
                used = self._read()
        except (BudgetError, OSError, TimeoutError) as e:
            return BudgetStatus(self.limit, self.limit, 0, error=str(e))
        return BudgetStatus(used, self.limit, max(0, self.limit - used))
