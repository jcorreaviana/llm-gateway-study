"""Tabela de recursos (gateway próprio x LiteLLM) com a forma de verificação de cada linha.

A coluna "como foi verificado" só aceita os quatro valores de `METHODS`: nada é afirmado sem dizer de onde veio.
O conteúdo das linhas vem do Marco 14 (experimentos em `scripts/compare_gateways.py`, leitura do código
instalado em `.venv-litellm/` e documentação oficial). O que não foi verificado diz "não verificado".
"""

from __future__ import annotations

from dataclasses import dataclass

METHODS = ("experimento", "código instalado", "documentação", "não verificado")


@dataclass(frozen=True)
class Feature:
    name: str
    own: str
    litellm: str
    method: str            # um de METHODS (a mesma forma vale para as duas colunas, salvo dito em `note`)
    note: str = ""

    def __post_init__(self) -> None:
        if self.method not in METHODS:
            raise ValueError(f"forma de verificacao invalida para {self.name!r}: {self.method!r}")


OUT_OF_SCOPE = "fora do escopo da comparação; o LiteLLM é um produto muito mais amplo, não avaliado aqui"

FEATURES: tuple[Feature, ...] = (
    Feature("Fallback entre provedores na ordem configurada",
            "sim (`chain`)", "sim (`router_settings.fallbacks`)", "experimento", "cenas 3 a 9"),
    Feature("Motivo do fallback na resposta",
            "header `x-gateway-fallback-reason` com o motivo por provedor, inclusive os pulados sem chamada",
            "header `x-litellm-attempted-fallbacks` só com a contagem; o motivo não vem em header, e os headers "
            "que registrei (`x-litellm-model-id`, `-attempted-fallbacks`, ...) não aparecem nas respostas de erro; nos "
            "erros o motivo vem em texto livre no corpo",
            "experimento", "cenas 3, 7 e 8"),
    Feature("Corpo do erro quando tudo falha",
            "503 estruturado, com `error.providers` (motivo por provedor) e `Retry-After`",
            "o status do primeiro erro (429 na cena 7), uma mensagem única em texto livre que cita os grupos; "
            "sem `Retry-After`", "experimento", "cena 7"),
    Feature("Limite local de requisições por minuto (rpm)",
            "balde de 10 que reabastece 1 a cada 6 s; 10 chamadas chegaram ao mock",
            "`rpm: 10` por minuto; 9 chamadas chegaram ao mock e as demais foram para o fallback, antes de "
            "chamar o mock", "experimento", "cena 2; o motivo de 9 e não 10 não foi investigado"),
    Feature("Respeito ao `Retry-After` de um 429",
            "sim: o cooldown dura o valor do header (2,0 a 2,2 s e 15,2 a 15,4 s)",
            "com `allowed_fails: 3` (config do Marco 13) um 429, ou mesmo 9 seguidos, não abriu cooldown; com "
            "`allowed_fails: 2` o cooldown durou o valor do header (2,2 s e 15,3 a 15,4 s), não o "
            "`cooldown_time` de 10 s", "experimento", "cenas 3a a 3d"),
    Feature("Limite de falhas até parar de chamar o provedor",
            "3 falhas CONSECUTIVAS (um sucesso zera a contagem)",
            "contagem por janela de tempo, sem zerar com sucesso; ver o achado sobre `allowed_fails` (o valor 3 é "
            "o padrão e ficou inativo)", "experimento", "cenas 4, 5a e 5b; leitura de "
            "`router_utils/cooldown_handlers.py` confirma a janela"),
    Feature("Recuperação do provedor",
            "meia-abertura com sonda controlada: uma chamada passa e as concorrentes vão ao fallback",
            "volta direto depois do cooldown: todas as chamadas concorrentes foram ao mock", "experimento",
            "cena 6, com o mock lento"),
    Feature("Janela de contexto do provedor",
            "pula o provedor antes de chamar, com estimativa de caracteres/4",
            "pula antes de chamar (`enable_pre_call_checks`), contando tokens com o tokenizador da biblioteca "
            "(9822 e 3574 tokens contra 11000 e 4000 da estimativa chars/4); responde 400, não 503",
            "experimento", "cenas 8a e 8b; a diferença das estimativas só foi medida nestes dois prompts"),
    Feature("Forçar um provedor",
            "header `x-gateway-force-provider`; sem fallback; nome desconhecido dá 400",
            "pedir o grupo pelo nome do modelo (`model: ollama_qwen`) funciona; para o grupo com fallbacks, "
            "`disable_fallbacks: true` no corpo desliga o fallback; não há header equivalente",
            "experimento", "cena 9"),
    Feature("Estado do gateway (status)",
            "`GET /status` com breaker, baldes e cooldown restante por provedor",
            "`/health/liveliness`, `/health/readiness`, `/health` (lista endpoints saudáveis), `/model/info`; "
            "sem endpoint com o estado de cooldown (`/cooldown` dá 404)", "experimento"),
    Feature("Métricas Prometheus",
            "`GET /metrics` com 9 famílias (`gateway_requests_total`, `gateway_fallback_total`, "
            "`gateway_breaker_state`, `gateway_latency_seconds`, `gateway_budget_remaining`, ...)",
            "`/metrics` dá 404 no modo só-config; com o callback `prometheus` o proxy NÃO sobe no ambiente "
            "travado por hash, porque `prometheus_client` não está no lock; quais métricas ele exporia "
            "não foi verificado", "experimento",
            "o código do logger cita usuários `premium` (nos filtros de rótulos); se há métricas só enterprise "
            "não foi verificado"),
    Feature("Gravação de requisições e respostas em arquivo",
            "recorder em JSONL (`record_content` liga ou desliga o conteúdo)",
            "não encontrei logger de arquivo embutido; as integrações embutidas enviam a serviços remotos ou "
            "usam banco; um callback customizado em Python seria possível (não testado)", "código instalado",
            "busca por escrita em arquivo em `integrations/`, `litellm_core_utils/` e `proxy/`"),
    Feature("Orçamento ou contador de chamadas",
            "orçamento de chamadas em arquivo (`data/budget.json`), reservado antes da chamada",
            "`max_budget` do proxy é em custo, e o código diz que sem banco o orçamento NÃO é aplicado e "
            "nenhuma requisição é bloqueada", "código instalado", "aviso em `proxy_server.py`; não executado"),
    Feature("Limite de TPM e `timeout` por tentativa",
            "TPM no balde local; `timeout_s` por tentativa", "existem no config (`tpm`, `timeout`)",
            "não verificado", "a demo usa `tpm: null` e nenhuma cena mede o timeout"),
    Feature("Streaming", "não suportado (400)", "não avaliado",
            "não verificado", OUT_OF_SCOPE),
    Feature("Outros provedores de LLM", "mock, Ollama e Anthropic (fail-closed)", "não avaliado",
            "não verificado", OUT_OF_SCOPE),
    Feature("Chaves virtuais, UI e banco", "não existe", "não avaliado",
            "não verificado", "fora do escopo da comparação; o LiteLLM é um produto muito mais amplo, não avaliado "
            "aqui (só `/global/spend` e `/spend/logs` mostraram que dependem de banco)"),
)
