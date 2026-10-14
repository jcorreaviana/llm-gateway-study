# llm-gateway-study

> **Projeto de estudo, sem garantias.** Código didático para entender roteamento
> com fallback em um LLM Gateway. Não use em produção. Licença MIT.

Laboratório em Python: um gateway que simula rate limits de LLMs de mercado e, por
gatilhos (429, 5xx/timeout, token bucket, circuit breaker, latência), redireciona
as requisições para modelos locais (Qwen/Llama via Ollama).

## Como rodar

```bash
python -m venv .venv
.venv\Scripts\activate          # Windows
pip install -r requirements.txt
copy .env.example .env          # preencha localmente; .env nunca é versionado
pytest
```

Testes rodam 100% contra mocks e não precisam de credenciais.
Chamadas reais à API só acontecem com `ENABLE_REAL_API=1` no `.env` (padrão `0`).

## Testes, cobertura e lint

```bash
pytest                          # suíte padrão (sem integração), em ordem aleatória (pytest-randomly)
pytest -p no:randomly           # ordem fixa, se precisar depurar
pytest --cov                    # cobertura de gateway/ e mock_upstream/ (falha abaixo de 85%)
pytest -m integration           # precisa do Ollama real em localhost:11434 (qwen2.5:3b)
pytest -p randomly --randomly-seed=1234   # reproduz uma ordem específica
ruff check .                    # lint
```

- O mapa requisito -> teste está em [docs/TEST_MATRIX.md](docs/TEST_MATRIX.md).
- Os testes nunca falam com a internet: uma fixture `autouse` (`tests/netguard.py`) só deixa abrir
  conexão para `127.0.0.1`/`localhost`/`::1` e falha o teste se algo tentar sair, mesmo que o código
  engula o erro. Nenhum teste escreve em `data/` nem `logs/` do repositório (`tmp_path`; a sessão falha
  se isso mudar). A cobertura atual é ~97%.
- Os testes de integração (`@pytest.mark.integration`) ficam fora da execução padrão. Sem o Ollama,
  eles fazem `skip` com uma mensagem clara. Eles sobem o mock e o gateway como processos reais em
  portas livres e encerram tudo no final. Para isso os pontos de entrada aceitam `MOCK_UPSTREAM_PORT` e
  `GATEWAY_CONFIG` (caminho de outro YAML, usado com
  `uvicorn gateway.main:app_factory --factory --host 127.0.0.1 --port N`).
- O CI (`.github/workflows/ci.yml`) roda `check_secrets.py --history`, `ruff` e `pytest --cov` em
  Python 3.12, sem integração e sem nenhuma variável de segredo.

## Checagem de segredos

```bash
python scripts/check_secrets.py             # arquivos versionados e novos
python scripts/check_secrets.py --history   # inclui todo o histórico do git
```

Retorna código 1 se achar chaves, e-mails pessoais ou caminhos absolutos do Windows.
A saída nunca mostra o valor encontrado.

### Hook de pre-commit

```bash
python scripts/install_hooks.py
```

Instala `.git/hooks/pre-commit`, que roda o `check_secrets.py` e **bloqueia o commit** se o
scan falhar. O script é versionado; o hook instalado não (rode de novo em cada clone).

## Mock upstream (o "provedor de mercado" simulado)

Servidor FastAPI que imita `POST /v1/chat/completions` (formato OpenAI) e pode ser
sabotado em tempo de execução. Sem modelo real, sem rede externa, sem disco. Escuta
**somente em `127.0.0.1`**; a porta vem de `mock_upstream.port` no `config.yaml` (padrão 9001).

```bash
python -m mock_upstream.server
```

A resposta é um eco determinístico do último prompt, prefixado com `[mock]`, com `usage`.

### API de chaos

| Endpoint | O que faz |
|---|---|
| `POST /chaos` | Define o comportamento. **Substitui** a config inteira: campos omitidos voltam ao padrão. |
| `GET /chaos` | Mostra a config atual. |
| `POST /chaos/reset` | Volta ao modo `normal`. |
| `GET /stats` | Requisições recebidas no chat, total e por status devolvido. |
| `POST /stats/reset` | Zera os contadores. |
| `GET /health` | `{"status": "ok"}`. |

Campos de `POST /chaos` (valores inválidos retornam 422):

| Campo | Padrão | Descrição |
|---|---|---|
| `mode` | `normal` | `normal`, `rate_limit` (429 + `Retry-After`), `server_error`, `slow` |
| `retry_after_seconds` | `1` | Valor do header `Retry-After` (>= 0) |
| `error_status` | `500` | Status do `server_error`: `500` ou `503` |
| `latency_ms` | `0` | Espera do modo `slow` (>= 0) |
| `fail_every_n` | `null` | Falha a cada N-ésima requisição, em qualquer modo (>= 1). O contador reinicia a cada `POST /chaos` |
| `fail_status` | `500` | Status da falha periódica (400 a 599; 429 inclui `Retry-After`) |

```bash
curl -X POST localhost:9001/chaos -H "content-type: application/json" \
     -d '{"mode": "rate_limit", "retry_after_seconds": 5}'
curl localhost:9001/stats
curl -X POST localhost:9001/chaos/reset
```

Erros seguem o formato OpenAI: `{"error": {"message", "type", "code"}}`.

## Gateway

Recebe `POST /v1/chat/completions` (formato OpenAI) e tenta os provedores da `chain`
em ordem, caindo para o próximo quando o atual falha. Escuta **somente em `127.0.0.1`**
(porta em `gateway.port`, padrão 8000). Streaming (`stream=true`) não é suportado (400).

```bash
python -m mock_upstream.server      # terminal 1
python -m gateway.main              # terminal 2
curl localhost:8000/v1/chat/completions -H "content-type: application/json" \
     -d '{"messages":[{"role":"user","content":"ola"}]}' -i
```

O `model` enviado pelo cliente é aceito mas substituído pelo `model` do provedor.

### Configuração (`config.yaml`)

| Chave | Descrição |
|---|---|
| `gateway.port` | Porta do gateway (padrão 8000). O bind não é configurável. |
| `gateway.force_provider_enabled` | Padrão `false`. Se `true`, vale o header `x-gateway-force-provider`. |
| `chain` | Ordem de tentativa, por nome de provedor. |
| `providers[]` | `name`, `type` (`mock`/`ollama`/`anthropic`), `base_url`, `model`, `timeout_s`, `context_window` (tokens), `enabled`. |
| `budget` | `limit` (padrão 30) e `path` (padrão `data/budget.json`). |
| `recorder` | `path` (padrão `logs/requests.jsonl`) e `record_content` (padrão `true`). |
| `providers[].rate_limit`, `.breaker`, `.default_retry_after_s`, `.warmup` | Resiliência, opcionais (veja abaixo). |

Validado com pydantic na inicialização: nome duplicado, nome inexistente ou repetido na
`chain`, e campos desconhecidos (como `api_key`) falham com mensagem clara.
Segredos nunca ficam no YAML. O provedor `anthropic` vem `enabled: false` e é **fail-closed**
(veja "Chamadas reais e budget"). `budget` e `recorder` também são seções do YAML.

### Roteamento e falhas

| Situação | Resultado |
|---|---|
| 429 / 5xx / timeout / erro de conexão | `rate_limited` / `server_error` / `timeout` / `connection_error`: fallback |
| 401 / 403 | `auth_error`: fallback e aviso no log (provedor mal configurado) |
| 2xx sem JSON válido | `invalid_response`: fallback |
| Provedor desabilitado | `disabled`: pulado |
| Prompt não cabe em `context_window` | `context_too_large`: pulado. Estimativa **aproximada**: caracteres/4. Nos Ollama o valor fica abaixo do contexto carregado (4096), para sobrar espaço à resposta |
| Anthropic bloqueada (flag, chave, budget, tamanho) | `real_api_blocked` / `budget_exhausted` / `input_too_large`: fallback, **sem** contar para o breaker |
| Outro 4xx | Erro do cliente: devolvido como veio, **sem** fallback |
| Todos falharam | 503 com `error.providers` = `{provedor: motivo}` |

Headers da resposta: `x-gateway-provider` (quem respondeu) e `x-gateway-fallback-reason`
(`provedor:motivo,...` dos pulados/falhos; ausente sem fallback).
`x-gateway-force-provider: <nome>` tenta só aquele provedor, sem fallback, e é ignorado
se `force_provider_enabled` for `false`. `GET /health` responde `{"status": "ok"}`.

Logs de aplicação registram apenas `request_id`, provedor, motivo e latência: nunca
prompt, resposta ou chaves.

### Resiliência (token bucket, circuit breaker e cooldown)

O router decide se um provedor está disponível **antes** de tentar (`Router.check_available`).
Ordem em cada tentativa: desabilitado -> contexto grande -> cooldown -> breaker -> token bucket -> chamada.

```yaml
providers:
  - name: mock_market
    # ...
    rate_limit: {rpm: 5, tpm: null}   # null desativa; sem a chave = sem limite
    breaker: {failure_threshold: 3, cooldown_s: 30, half_open_max_calls: 1}   # estes são os padrões
    default_retry_after_s: 30         # usado em 429 sem header Retry-After
    warmup: false                     # só Ollama: requisição mínima (max_tokens=1) na inicialização
```

Todos os números devem ser > 0 (zero ou negativo falha na inicialização). Mock e Ollama vêm
sem limite; o `config.yaml` de exemplo deixa o `mock_market` com `rpm: 5` para a demo.

| Mecanismo | Comportamento | Motivo ao pular |
|---|---|---|
| Token bucket | Um balde de RPM e outro de TPM por provedor, com reabastecimento **contínuo**. TPM usa a estimativa chars/4 e, em sucesso, é corrigido para `usage.total_tokens` real. Um pedido maior que o balde passa uma vez, com o balde cheio. | `rate_limit_local` |
| Circuit breaker | `closed` -> `open` após N falhas **consecutivas** (`server_error`, `timeout`, `connection_error`, `invalid_response`, `auth_error`); um sucesso zera. Após `cooldown_s`, `half_open` deixa passar até `half_open_max_calls` sondas concorrentes: sucesso fecha, falha reabre e reinicia o cooldown. `rate_limited`, `client_error`, `context_too_large`, `rate_limit_local`, `real_api_blocked`, `budget_exhausted` e `input_too_large` não contam. | `breaker_open` |
| Cooldown | Um 429 marca o provedor indisponível por `Retry-After` (ou `default_retry_after_s`), sem chamá-lo. Não conta para o breaker. | `cooling_down` |

Se todos estiverem indisponíveis, a resposta é 503 com `error.providers` e o header
`Retry-After` (segundos, arredondado para cima) com a **menor** espera entre os provedores que
informaram uma; sem nenhuma, o header é omitido. `x-gateway-force-provider` também respeita
cooldown, breaker e bucket. Os novos motivos aparecem em `x-gateway-fallback-reason`.

O timeout por provedor (`timeout_s`) é o prazo total da tentativa; ao estourar, a requisição é
cancelada de verdade (`asyncio.timeout`) e conta como `timeout` para o breaker. Com `warmup: true`,
o gateway faz uma requisição mínima ao modelo Ollama na inicialização, para a carga a frio não ser
confundida com falha (se falhar, só avisa no log; não passa pelo breaker).

### `GET /status`

Somente leitura e sem segredos (nada de `base_url` ou chaves). Para cada provedor: `type`,
`enabled`, `breaker` (`state`, `consecutive_failures`, `open_remaining_s`), `buckets`
(`rpm`/`tpm` com `level` e `capacity`, ou `null` sem limite) e `cooldown_remaining_s`.
No topo: `budget` (`used`, `limit`, `remaining`, mais `error` se o arquivo estiver inválido) e
`real_api_enabled` (somente o booleano de `ENABLE_REAL_API=1`).

```bash
curl localhost:8000/status
```

## Chamadas reais e budget

O `AnthropicProvider` só abre conexão se **todas** as condições forem verdadeiras:

1. `ENABLE_REAL_API=1` (padrão `0` se ausente; o `.env` é carregado com python-dotenv);
2. `ANTHROPIC_API_KEY` presente (lida do ambiente só no momento do uso);
3. provedor com `enabled: true` no `config.yaml`;
4. entrada estimada <= `real_api_max_input_tokens` (padrão 1000, estimativa chars/4);
5. `budget.reserve()` com sucesso.

Qualquer falha devolve um motivo tipado **sem abrir conexão**: `real_api_blocked`, `input_too_large`
ou `budget_exhausted`, todos com fallback e sem tocar o breaker. As checagens baratas vêm antes da
reserva, então um bloqueio não gasta orçamento. O host (`api.anthropic.com/v1/messages`) é fixo no
código; o `model` vem do config (`claude-haiku-5-5`), nunca do request; `max_tokens` efetivo =
`min(request, max_tokens_cap)` (padrão 50, e o cap vale se o request não informar). A mensagem
`system` vira o campo `system`; a resposta é traduzida para o formato OpenAI.

**Budget** (`data/budget.json`, fora do git): contador persistente com limite rígido (padrão 30).
A reserva acontece **antes** de enviar e nunca é devolvida: falha, timeout e erro também contam, então
um loop com erro não passa do limite. Lock de arquivo (`msvcrt` no Windows, `fcntl` no resto) e
escrita atômica (`os.replace`) tornam seguro entre tarefas e processos. É fail-closed: arquivo
corrompido, ilegível ou com valores inválidos conta como esgotado e **nunca** é reiniciado em
silêncio. Não há reset automático; o manual é explícito e pede confirmação:

```bash
python scripts/reset_budget.py
```

A chave nunca aparece em logs, exceções, `/status`, `/metrics` nem no recorder (qualquer padrão
`sk-...` e o valor da chave do ambiente são mascarados). Os testes usam só `httpx.MockTransport`.

## Métricas (`GET /metrics`)

Formato Prometheus, com um registry próprio por app. Labels de baixa cardinalidade (nunca
`request_id`, prompt ou modelo livre).

| Métrica | Labels | Significado |
|---|---|---|
| `gateway_requests_total` | `provider`, `status` | uma por requisição; `provider="none"` no 503 e nos 400 do gateway |
| `gateway_fallback_total` | `from_provider`, `to_provider`, `reason` | cada provedor pulado/falho; `to_provider` é quem respondeu (`none` no 503) |
| `gateway_latency_seconds` | `provider` | histograma, só de chamadas efetivamente feitas |
| `gateway_breaker_state` | `provider` | 0 closed, 1 half_open, 2 open; calculado no scrape (reflete a transição por tempo) |
| `gateway_real_api_calls_total` | — | chamadas reais (reservas do budget) |
| `gateway_budget_remaining` | — | chamadas restantes |

## Recorder (`logs/requests.jsonl`)

Uma linha JSON por requisição (inclusive 503 e erros de cliente), insumo da Fase 3. O arquivo guarda
prompts e respostas, por isso fica em `logs/` (ignorado pelo git). Campos: `request_id` (uuid4, o
mesmo do header `x-gateway-request-id`), `timestamp` (UTC), `messages`, `temperature`, `max_tokens`,
`provider` (quem respondeu ou `none`), `forced_provider`, `fallback_reason`, `status`, `latency_ms`,
`usage` e `response` (texto). Com `record_content: false` só vão os metadados. Padrões de segredo são
mascarados. Uma falha de gravação nunca derruba a requisição (só um aviso, sem conteúdo). Os logs de
aplicação continuam sem prompt, resposta ou chave.

*(A demo completa será adicionada nos próximos marcos.)*
