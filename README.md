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

## Checagem de segredos

```bash
python scripts/check_secrets.py             # arquivos versionados e novos
python scripts/check_secrets.py --history   # inclui todo o histórico do git
```

Retorna código 1 se achar chaves, e-mails pessoais ou caminhos absolutos do Windows.
A saída nunca mostra o valor encontrado. Opcional, como pre-commit:

```bash
echo python scripts/check_secrets.py > .git/hooks/pre-commit
```

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

Validado com pydantic na inicialização: nome duplicado, nome inexistente ou repetido na
`chain`, e campos desconhecidos (como `api_key`) falham com mensagem clara.
Segredos nunca ficam no YAML. O provedor `anthropic` vem `enabled: false` e **está
fail-closed neste estágio**: recusa qualquer chamada real (`real_api_blocked`), mesmo com
`ENABLE_REAL_API=1` e chave. A liberação depende do budget (Marco 5).

### Roteamento e falhas

| Situação | Resultado |
|---|---|
| 429 / 5xx / timeout / erro de conexão | `rate_limited` / `server_error` / `timeout` / `connection_error`: fallback |
| 401 / 403 | `auth_error`: fallback e aviso no log (provedor mal configurado) |
| 2xx sem JSON válido | `invalid_response`: fallback |
| Provedor desabilitado | `disabled`: pulado |
| Prompt não cabe em `context_window` | `context_too_large`: pulado. Estimativa **aproximada**: caracteres/4 |
| Outro 4xx | Erro do cliente: devolvido como veio, **sem** fallback |
| Todos falharam | 503 com `error.providers` = `{provedor: motivo}` |

Headers da resposta: `x-gateway-provider` (quem respondeu) e `x-gateway-fallback-reason`
(`provedor:motivo,...` dos pulados/falhos; ausente sem fallback).
`x-gateway-force-provider: <nome>` tenta só aquele provedor, sem fallback, e é ignorado
se `force_provider_enabled` for `false`. `GET /health` responde `{"status": "ok"}`.

Logs de aplicação registram apenas `request_id`, provedor, motivo e latência: nunca
prompt, resposta ou chaves. Token bucket e circuit breaker entram no Marco 4
(`Router.check_available` é o ponto de extensão).

*(As seções de métricas e demo serão adicionadas nos próximos marcos.)*
