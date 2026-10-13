"""Carrega e valida o config.yaml do gateway.

Segredos nunca entram aqui: campos desconhecidos são rejeitados (extra="forbid"), então
um `api_key:` no YAML falha na inicialização. A chave da Anthropic vem só do ambiente.
"""

from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

HOST = "127.0.0.1"  # fixo de propósito: o gateway nunca fica exposto na rede
DEFAULT_PORT = 8000
CONFIG_PATH = Path(__file__).resolve().parent.parent / "config.yaml"


class GatewaySettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    port: int = Field(default=DEFAULT_PORT, ge=1, le=65535)
    force_provider_enabled: bool = False


class MockUpstreamSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    port: int = Field(default=9001, ge=1, le=65535)


class RateLimitConfig(BaseModel):
    """Limites locais por minuto. `null` (ou omitido) desativa o respectivo balde."""

    model_config = ConfigDict(extra="forbid")

    rpm: int | None = Field(default=None, gt=0)
    tpm: int | None = Field(default=None, gt=0)


class BreakerConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    failure_threshold: int = Field(default=3, gt=0)
    cooldown_s: float = Field(default=30.0, gt=0)
    half_open_max_calls: int = Field(default=1, gt=0)


class ProviderConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1)
    type: Literal["mock", "ollama", "anthropic"]
    base_url: str | None = None
    model: str = Field(min_length=1)
    timeout_s: float = Field(default=30.0, gt=0)
    context_window: int = Field(gt=0)  # em tokens
    enabled: bool = True
    rate_limit: RateLimitConfig | None = None  # sem limite por padrão
    breaker: BreakerConfig = BreakerConfig()
    default_retry_after_s: float = Field(default=30.0, gt=0)  # 429 sem header Retry-After
    warmup: bool = False  # só Ollama: uma requisição mínima na inicialização

    @model_validator(mode="after")
    def _base_url_required(self) -> "ProviderConfig":
        if self.type in ("mock", "ollama") and not self.base_url:
            raise ValueError(f"provedor '{self.name}' ({self.type}) exige base_url")
        if self.warmup and self.type != "ollama":
            raise ValueError(f"provedor '{self.name}': warmup só vale para type ollama")
        return self


class AppConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    gateway: GatewaySettings = GatewaySettings()
    mock_upstream: MockUpstreamSettings = MockUpstreamSettings()
    chain: list[str] = Field(min_length=1)
    providers: list[ProviderConfig] = Field(min_length=1)

    @model_validator(mode="after")
    def _check_names(self) -> "AppConfig":
        names = [p.name for p in self.providers]
        dupes = sorted({n for n in names if names.count(n) > 1})
        if dupes:
            raise ValueError(f"nomes de provedor duplicados: {', '.join(dupes)}")
        unknown = [n for n in self.chain if n not in names]
        if unknown:
            raise ValueError(
                f"chain referencia provedor inexistente: {', '.join(unknown)} "
                f"(definidos: {', '.join(names)})"
            )
        repeated = sorted({n for n in self.chain if self.chain.count(n) > 1})
        if repeated:
            raise ValueError(f"chain repete provedor: {', '.join(repeated)}")
        return self

    def provider(self, name: str) -> ProviderConfig:
        return next(p for p in self.providers if p.name == name)


def load_config(path: Path | str = CONFIG_PATH) -> AppConfig:
    """Lê e valida o YAML. Levanta pydantic.ValidationError com mensagem clara."""
    raw = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    return AppConfig.model_validate(raw)
