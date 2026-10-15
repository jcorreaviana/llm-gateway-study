# Fase 2: comparação com o LiteLLM Proxy

Estado: **Marco 14 concluído** (comportamento comparado por experimento nas mesmas cenas; seções 4 a 8).
O Marco 13 fez a instalação isolada, o config equivalente e o smoke (seções 1 a 3). **Nenhuma chamada à Anthropic**
em nenhum processo (`ANTHROPIC_API_KEY` vazia, nenhum provedor Anthropic no config).

> **Os resultados desta comparação valem para a versão testada (LiteLLM 1.103.1, lançada em 29/09/2026) e não
> devem ser generalizados para outras versões**, em especial a de março (1.83.0), que tem achados de segurança
> abertos. Extensão futura possível: repetir a comparação com uma versão mais nova.

## 1. Versão e verificação da cadeia de suprimentos

- **LiteLLM 1.103.1**, lançado em 29/09/2026. Lock com hashes do PyPI (`requirements-litellm.lock.txt`, 116
  pacotes, todos com mais de 14 dias na data do lock, gerado com `uv --generate-hashes --no-build`; instalado com
  `--require-hashes --only-binary=:all: --no-deps`). Entrada: `requirements-litellm.txt`. Fora do
  `requirements.txt` e do CI; só no `.venv-litellm/` (ignorado pelo git).
- **Os hashes vêm do PyPI, não da tabela oficial de SHA-256 do aviso de segurança de março** (essa tabela cobre só
  as versões 1.78.0 a 1.82.6).
- **`pip-audit` sobre o lock (executado fora do `.venv-litellm`, em 15/10/2026): sem vulnerabilidades conhecidas
  na data do lock.**
- **A 1.83.0 foi descartada.** Era a versão recomendada pelo aviso oficial, mas o `pip-audit` achou 11 avisos de
  segurança (26 achados contando duplicatas) em 2 pacotes, incluindo um bypass de autenticação por header `Host`
  (CVE-2026-49468, corrigido na 1.84.0) e RCE autenticado em `/prompts/test` (CVE-2026-42203, corrigido na 1.83.7).
- `httpx2` e `httpcore2` (dependências transitivas novas) foram verificados: PyPI aponta para
  `github.com/pydantic/httpx2` (organização `pydantic`, mantenedor Pydantic Services Inc., autor Tom Christie), upload
  em 23/09/2026; exigidos por `litellm` e `mcp`.
- Pós-instalação: `pip check` sem conflitos; `pip freeze` igual ao lock (116 pacotes, só o `pip` a mais); o único
  `.pth` é o `pywin32.pth` (importa `pywin32_bootstrap`, que só chama `os.add_dll_directory`); nenhum `sitecustomize`,
  `usercustomize` ou `litellm_init`.
- **Superfície aceita:** o `litellm[proxy]` instala SDKs de nuvem (boto3, azure-*, msal), `polars`, `mcp`, Redis/`rq` e
  outros que NÃO usamos. Eles só conectam se um provedor ou serviço for configurado. Nenhum pacote de telemetria ou
  analytics (só `opentelemetry-api`, sem exporter).

## 2. Isolamento do smoke

- Ambiente mínimo (nenhuma variável do usuário é herdada), diretório de trabalho temporário, bind em `127.0.0.1`,
  modo só-config (sem banco, sem UI), master key aleatória gerada a cada execução (nunca versionada).
- Telemetria: a flag real é `--telemetry False`. **`LITELLM_TELEMETRY` não existe nesta versão** (confirmado no código
  instalado) e `litellm.telemetry` não é lida por nada. Variáveis usadas e confirmadas no código: `DO_NOT_TRACK`,
  `HF_HUB_OFFLINE`, `LITELLM_LOCAL_MODEL_COST_MAP`, `LITELLM_LOCAL_BLOG_POSTS` (e as demais `LITELLM_LOCAL_*`),
  `DISABLE_ADMIN_UI`, `NO_DOCS`, `NO_REDOC`, `CUSTOM_TIKTOKEN_CACHE_DIR` (o LiteLLM sobrescreve `TIKTOKEN_CACHE_DIR`).
- **Três camadas contra saída de rede:** (1) audit hook em `litellm/netguard/sitecustomize.py` (via `PYTHONPATH`),
  que registra e bloqueia, levantando exceção ANTES da chamada de sistema, qualquer socket ou resolução de nome fora
  do loopback, mais verificação em `asyncio` (no Windows o Proactor conecta a IP literal sem gerar evento de
  auditoria); (2) proxy sumidouro (`HTTP(S)_PROXY` para uma porta local sem serviço; conectar a ela é violação);
  (3) monitor externo das conexões TCP do PID do proxy e dos filhos.
- **Limitação conhecida:** o audit hook só vê sockets abertos pelo Python. Extensões nativas (`hf-xet`, `granian`,
  `tiktoken`, `tokenizers`, etc.) não passam por ele. A segunda camada para elas é o monitor externo, que só vê
  conexões TCP, em amostras.
- Hosts externos que o código do pacote pode contatar (levantamento por grep, informativo): `raw.githubusercontent.com`
  (templates de política), `api.litellm.ai` (gerenciador de budget), `license.litellm.ai` (licença),
  `huggingface.co` (tokenizers), `openaipublic.blob.core.windows.net` (tiktoken, ver abaixo) e as APIs dos provedores
  de LLM. Com `LITELLM_LOCAL_*` e a config local, nenhuma foi contatada no smoke.

### Achado: o proxy tentou baixar o `cl100k_base` na inicialização

Com o LiteLLM 1.103.1 no Windows, o proxy tentou baixar o `cl100k_base` na inicialização porque os arquivos de cache
embutidos no wheel estão com CRLF e a verificação de SHA-256 do `tiktoken` os rejeita; o conteúdo normalizado bate
com o hash oficial. Contorno usado: cache temporário com LF, conferido por hash (`scripts/_tiktoken_cache.py`: hash
esperado lido do `tiktoken_ext/openai_public.py` instalado, nome do arquivo = SHA-1 da URL como em
`tiktoken.load.read_file_cached`; aborta se qualquer hash não bater; nada dentro do `.venv-litellm` é alterado).
**Observado só neste ambiente e nesta versão; não testado em outros sistemas.**

**A trava bloqueou a tentativa de saída antes de a conexão acontecer** (o destino, obtido do traceback, era
`openaipublic.blob.core.windows.net`; o log da trava só mostrava a porta do sumidouro). Foi o único bloqueio; depois do
contorno, nenhum.

## 3. Smoke (mock `normal`, depois `rate_limit`; prompt genérico, `max_tokens` 8)

- (a) mock `normal`: 200, `x-litellm-model-id: mock_market`, `x-litellm-attempted-fallbacks: 0`.
- (b) mock `rate_limit` (payload completo no `POST /chaos`): 200 vindo do Qwen, `x-litellm-model-id: ollama_qwen`,
  `x-litellm-attempted-fallbacks: 1`. `/stats` do mock: 2 recebidas (200:1, 429:1).
- Headers do LiteLLM que indicam o modelo usado: `x-litellm-model-id`, `x-litellm-model-group`,
  `x-litellm-model-api-base`, `x-litellm-model-name`, `x-litellm-attempted-fallbacks`, `x-litellm-attempted-retries`.
  O header do LiteLLM NÃO diz o motivo do fallback (o gateway do projeto diz: `x-gateway-fallback-reason`).
  `x-ratelimit-limit-requests: 10` apareceu na resposta do `mock_market`.
- Trava: 0 eventos bloqueados; monitor externo: 0 conexões fora do loopback; sem processo restante.
- Latências do smoke não são comparáveis (a primeira chamada ao Qwen carregou o modelo; a primeira chamada ao proxy
  inclui inicialização preguiçosa).

## 4. Método da comparação (Marco 14)

- **O que é:** o mesmo mock, o mesmo Ollama (`qwen2.5:3b` e `llama3.2:3b`) e as mesmas cenas nos dois gateways, um por
  vez (o próprio, depois o LiteLLM). **Não é benchmark.** Runner: `scripts/compare_gateways.py`; tabelas e CSV:
  `python -m comparekit.report`. Cada requisição usa `temperature` 0, `max_tokens` 16 e prompts genéricos; os prompts
  grandes são texto genérico repetido, gerado na hora.
- **Estado zerado:** o gateway e o mock são reiniciados a cada cena e a cada repetição (3 repetições; os dois modelos
  locais foram aquecidos antes e essa rodada foi descartada; cada subida ainda faz uma requisição descartada ao Qwen).
  O `/chaos` e o `/stats` do mock são zerados no início de cada cena. A medida de eficácia do cooldown e do breaker é
  quantas chamadas CHEGARAM ao mock (`/stats`).
- **LiteLLM sempre sob a trava de rede:** as três camadas do Marco 13 (audit hook, proxy sumidouro e monitor de conexões)
  em todas as subidas do proxy; qualquer bloqueio pararia a execução. Resultado na seção 5.
- **Configs:** o gateway próprio usa `config.demo.yaml` (derivado, como na demo) e o LiteLLM usa
  `litellm/config.litellm.yaml`, sem alteração. Duas diferenças deliberadas, iguais nos dois gateways: (1) **o `rpm` do
  `mock_market` só existe na cena 2**; nas outras cenas foi removido, porque numa tentativa exploratória (descartada) o
  `rpm: 10` (janela de 1 minuto no LiteLLM) barrava a 10ª chamada ao mock e se confundia com o efeito do breaker; (2) na
  cena 7 os dois Ollama apontam para uma porta local sem serviço, porque derrubar o Ollama de verdade não era viável.
- **Qual coluna é a comparável:** a comparação principal nas cenas de falha é **gateway próprio x LiteLLM com o contador de
  falhas ativo** (`allowed_fails: 2`, cenas 3c, 3d, 4, 5a, 5b e 6). A coluna do config versionado (`allowed_fails: 3`) fica como
  observação secundária, rotulada "contador de falhas inativo: `allowed_fails: 3` = padrão da biblioteca"; nela, "não parou"
  é efeito dessa configuração, não uma limitação do produto. Motivo no achado 1.
- **Duas execuções do runner:** a principal (cenas 1 a 9, as três configurações) e uma complementar (cenas 3c e 3d), feita
  depois que a cena 3 mostrou que um único 429 não ativa o contador de falhas do LiteLLM. As duas usam o mesmo runner e a
  mesma trava. Uma terceira (cena 7b, com o mock em 500) respondeu à pergunta sobre o status do erro na cena 7. As sondas de recursos
  (seção 5) e uma execução de diagnóstico da cena 8b (uma repetição, só para ler a mensagem de erro completa; não entra
  nas tabelas) rodaram também sob a trava, que soma todas.
- **Medida de tempo:** "tempo até voltar ao mock" vai do fim da requisição que abriu o cooldown (ou da queda do mock) até
  o envio da primeira requisição que chegou ao mock; a resolução é o intervalo de sondagem (0,5 s nas cenas 3; 0,25 s na
  cena 6). Dispersão de até 1 s entre repetições é tratada como o mesmo resultado.
- **Cena 6 (recuperação):** o mock volta com 2 s de latência e o runner dispara uma sondagem a cada 0,25 s sem esperar a
  resposta; assim se vê quantas requisições o gateway deixa chegar ao provedor enquanto a primeira chamada ainda está em
  andamento (uma sonda controlada ou todas).
- **Cena 9:** no LiteLLM, forçar é tentar pelo nome do modelo no request, como pedido.
- **Fora do repositório:** os resultados brutos (com headers e texto de erro dos gateways) ficam em `compare_results/`,
  ignorado pelo git; o CSV e este documento só têm agregados.

## 5. Resultados

<!-- BEGIN GENERATED: comparekit.report (nao editar a mao; rode python -m comparekit.report) -->

> **Avisos obrigatórios**
>
> - Os resultados valem para o LiteLLM 1.103.1 (setembro/2026), em um Windows, no modo só-config, em um cenário específico com o mock, e não devem ser generalizados; versões diferentes podem se comportar de modo diferente.
> - A comparação é de COMPORTAMENTO num cenário, não de qualidade geral nem de desempenho.
> - O gateway próprio é um projeto de estudo e o LiteLLM é um produto muito mais completo e muito mais amplo, que não foi avaliado aqui além deste cenário.
> - Latências de uma máquina, sem concorrência e com poucas repetições são só indicativas.

Execução `m14-full`: 3 repetições por cena e por gateway, o gateway e o mock reiniciados a cada repetição; cada requisição usa `temperature` 0 e `max_tokens` 16.

**Como ler as tabelas**

- **Tentativas de fallback** não são chamadas feitas: no gateway próprio é o número de provedores listados em `x-gateway-fallback-reason` (inclui os pulados sem chamada, como `cooling_down` e `context_too_large`); no LiteLLM é o valor de `x-litellm-attempted-fallbacks`. A medida de quantas chamadas CHEGARAM ao provedor é a coluna do mock (`/stats` do mock, zerado a cada repetição).
- **Tempo até voltar ao mock**: do fim da requisição que provocou o cooldown (ou da queda do mock) até o envio da primeira requisição que chegou ao mock; a resolução é o intervalo de sondagem (0,5 s nas cenas 3a e 3b, 0,25 s na cena 6). Dispersão de até 1 s entre repetições é tratada como o mesmo resultado.
- **Coluna do config versionado do LiteLLM**: nas cenas de falha (3c, 3d, 4, 5a, 5b e 6) o contador de falhas está inativo (`allowed_fails: 3` é o padrão da biblioteca), então "não parou" ali é efeito dessa configuração, não uma limitação do produto; a coluna comparável ao gateway próprio é a de `allowed_fails: 2`. Nas cenas 1, 2, 3a, 3b, 7, 7b, 8a, 8b e 9 só o config versionado foi executado (o contador não está em jogo, ou um 429 só não o ativa).
- **Estabilidade**: as 3 repetições têm os mesmos status, provedores, tentativas e chamadas ao mock. Quando divergem, a tabela mostra cada valor com a contagem, sem escolher um.

### Comportamento por cena

| Cena | Gateway | Requisições (status) | Provedor que respondeu | Tentativas de fallback | Chamadas que chegaram ao mock | Tempo até voltar ao mock (s) | Estabilidade |
|---|---|---|---|---|---|---|---|
| 1: Normal | gateway próprio | 200 x3 | mock_market x3 | 0 | 3 | — | 3/3 iguais |
| 1: Normal | LiteLLM 1.103.1, config versionado (contador de falhas inativo: allowed_fails: 3 = padrão da biblioteca) | 200 x3 | mock_market x3 | 0 | 3 | — | 3/3 iguais |
| 2: Limite local (rpm 10, 13 requisições seguidas) | gateway próprio | 200 x13 | mock_market x10|ollama_qwen x3 | 3 | 10 | — | 3/3 iguais |
| 2: Limite local (rpm 10, 13 requisições seguidas) | LiteLLM 1.103.1, config versionado (contador de falhas inativo: allowed_fails: 3 = padrão da biblioteca) | 200 x13 | mock_market x9|ollama_qwen x4 | 4 | 9 | — | 3/3 iguais |
| 3a: 429 com Retry-After de 2 s | gateway próprio | 200 x6 | ollama_qwen x5|mock_market | 5 | 2 | 2.0 | 3/3 iguais |
| 3a: 429 com Retry-After de 2 s | LiteLLM 1.103.1, config versionado (contador de falhas inativo: allowed_fails: 3 = padrão da biblioteca) | 200 x2 | ollama_qwen|mock_market | 1 | 2 | 0.0 | 3/3 iguais |
| 3b: 429 com Retry-After de 15 s | gateway próprio | 200 x32 | ollama_qwen x31|mock_market | 31 | 2 | 15.2 | 3/3 iguais |
| 3b: 429 com Retry-After de 15 s | LiteLLM 1.103.1, config versionado (contador de falhas inativo: allowed_fails: 3 = padrão da biblioteca) | 200 x2 | ollama_qwen|mock_market | 1 | 2 | 0.0 | 3/3 iguais |
| 3c: 429 repetidos com Retry-After de 2 s (até 8 requisições) | gateway próprio | 200 x8 | ollama_qwen x7|mock_market | 7 | 2 | 2.1 a 2.2 | 3/3 iguais |
| 3c: 429 repetidos com Retry-After de 2 s (até 8 requisições) | LiteLLM 1.103.1 (allowed_fails: 2, comparável) | 200 x10 | ollama_qwen x9|mock_market | 9 | 4 | 2.2 | 3/3 iguais |
| 3c: 429 repetidos com Retry-After de 2 s (até 8 requisições) | LiteLLM 1.103.1, config versionado (contador de falhas inativo: allowed_fails: 3 = padrão da biblioteca) | 200 x9 | ollama_qwen x8|mock_market | 8 | 9 | 0.0 | 3/3 iguais |
| 3d: 429 repetidos com Retry-After de 15 s (até 8 requisições) | gateway próprio | 200 x34 | ollama_qwen x33|mock_market | 33 | 2 | 15.4 | 3/3 iguais |
| 3d: 429 repetidos com Retry-After de 15 s (até 8 requisições) | LiteLLM 1.103.1 (allowed_fails: 2, comparável) | 200 x36 | ollama_qwen x35|mock_market | 35 | 4 | 15.3 a 15.4 | 3/3 iguais |
| 3d: 429 repetidos com Retry-After de 15 s (até 8 requisições) | LiteLLM 1.103.1, config versionado (contador de falhas inativo: allowed_fails: 3 = padrão da biblioteca) | 200 x9 | ollama_qwen x8|mock_market | 8 | 9 | 0.0 | 3/3 iguais |
| 4: Falha 5xx contínua (10 requisições) | gateway próprio | 200 x10 | ollama_qwen x10 | 10 | 3 | — | 3/3 iguais |
| 4: Falha 5xx contínua (10 requisições) | LiteLLM 1.103.1 (allowed_fails: 2, comparável) | 200 x10 | ollama_qwen x10 | 10 | 3 | — | 3/3 iguais |
| 4: Falha 5xx contínua (10 requisições) | LiteLLM 1.103.1, config versionado (contador de falhas inativo: allowed_fails: 3 = padrão da biblioteca) | 200 x10 | ollama_qwen x10 | 10 | 10 | — | 3/3 iguais |
| 5a: Falhas intercaladas: 2 falhas, 1 sucesso, repetido 4 vezes | gateway próprio | 200 x12 | ollama_qwen x2|mock_market|ollama_qwen x2|mock_market|ollama_qwen x2|mock_market|ollama_qwen x2|mock_market | 8 | 12 | — | 3/3 iguais |
| 5a: Falhas intercaladas: 2 falhas, 1 sucesso, repetido 4 vezes | LiteLLM 1.103.1 (allowed_fails: 2, comparável) | 200 x12 | ollama_qwen x2|mock_market|ollama_qwen x9 | 11 | 4 | — | 3/3 iguais |
| 5a: Falhas intercaladas: 2 falhas, 1 sucesso, repetido 4 vezes | LiteLLM 1.103.1, config versionado (contador de falhas inativo: allowed_fails: 3 = padrão da biblioteca) | 200 x12 | ollama_qwen x2|mock_market|ollama_qwen x2|mock_market|ollama_qwen x2|mock_market|ollama_qwen x2|mock_market | 8 | 12 | — | 3/3 iguais |
| 5b: Falhas intercaladas: falha, sucesso, repetido 5 vezes | gateway próprio | 200 x10 | ollama_qwen|mock_market|ollama_qwen|mock_market|ollama_qwen|mock_market|ollama_qwen|mock_market|ollama_qwen|mock_market | 5 | 10 | — | 3/3 iguais |
| 5b: Falhas intercaladas: falha, sucesso, repetido 5 vezes | LiteLLM 1.103.1 (allowed_fails: 2, comparável) | 200 x10 | ollama_qwen|mock_market|ollama_qwen|mock_market|ollama_qwen x6 | 8 | 5 | — | 3/3 iguais |
| 5b: Falhas intercaladas: falha, sucesso, repetido 5 vezes | LiteLLM 1.103.1, config versionado (contador de falhas inativo: allowed_fails: 3 = padrão da biblioteca) | 200 x10 | ollama_qwen|mock_market|ollama_qwen|mock_market|ollama_qwen|mock_market|ollama_qwen|mock_market|ollama_qwen|mock_market | 5 | 10 | — | 3/3 iguais |
| 6: Recuperação (mock volta lento, sondagem a cada 0,25 s) | gateway próprio | 200 x60 | ollama_qwen x43|mock_market|ollama_qwen x7|mock_market x9 | 50 | 13 | 9.8 a 10.0 | 3/3 iguais |
| 6: Recuperação (mock volta lento, sondagem a cada 0,25 s) | LiteLLM 1.103.1 (allowed_fails: 2, comparável) | 200 x60 | ollama_qwen x43|mock_market x17 | 43 | 20 | 9.9 a 10.0 | 3/3 iguais |
| 6: Recuperação (mock volta lento, sondagem a cada 0,25 s) | LiteLLM 1.103.1, config versionado (contador de falhas inativo: allowed_fails: 3 = padrão da biblioteca) | 200 x27 | ollama_qwen x10|mock_market x17 | 10 | 27 | 0.0 | 3/3 iguais |
| 7: Tudo indisponível (Ollama apontado para porta sem serviço, mock em 429) | gateway próprio | 503 x2 | none x2 | 6 | 1 | — | 3/3 iguais |
| 7: Tudo indisponível (Ollama apontado para porta sem serviço, mock em 429) | LiteLLM 1.103.1, config versionado (contador de falhas inativo: allowed_fails: 3 = padrão da biblioteca) | 429 x2 | none x2 | 0 | 2 | — | 3/3 iguais |
| 7b: Tudo indisponível (Ollama apontado para porta sem serviço, mock em 500) | gateway próprio | 503 x2 | none x2 | 6 | 2 | — | 3/3 iguais |
| 7b: Tudo indisponível (Ollama apontado para porta sem serviço, mock em 500) | LiteLLM 1.103.1, config versionado (contador de falhas inativo: allowed_fails: 3 = padrão da biblioteca) | 500 x2 | none x2 | 0 | 2 | — | 3/3 iguais |
| 8a: Prompt maior que todas as janelas (~44 mil caracteres) | gateway próprio | 503|200 | none|mock_market | 3 | 1 | — | 3/3 iguais |
| 8a: Prompt maior que todas as janelas (~44 mil caracteres) | LiteLLM 1.103.1, config versionado (contador de falhas inativo: allowed_fails: 3 = padrão da biblioteca) | 400|200 | none|mock_market | 0 | 1 | — | 3/3 iguais |
| 8b: Prompt maior que a janela do Ollama, menor que a do mock (~16 mil caracteres, mock em 429) | gateway próprio | 503 | none | 3 | 1 | — | 3/3 iguais |
| 8b: Prompt maior que a janela do Ollama, menor que a do mock (~16 mil caracteres, mock em 429) | LiteLLM 1.103.1, config versionado (contador de falhas inativo: allowed_fails: 3 = padrão da biblioteca) | 429 | none | 0 | 1 | — | 3/3 iguais |
| 9: Forçar provedor | gateway próprio | 200|503|400 | ollama_qwen|none x2 | 1 | 1 | — | 3/3 iguais |
| 9: Forçar provedor | LiteLLM 1.103.1, config versionado (contador de falhas inativo: allowed_fails: 3 = padrão da biblioteca) | 200 x2|429|400 | ollama_qwen x2|none x2 | 1 | 2 | — | 3/3 iguais |

### Métricas derivadas

| Cena | Métrica | gateway próprio | LiteLLM 1.103.1 (allowed_fails: 2, comparável) | LiteLLM 1.103.1, config versionado (contador de falhas inativo: allowed_fails: 3 = padrão da biblioteca) |
|---|---|---|---|---|
| 3c | chamadas ao mock até o gateway parar de chamá-lo | 1 | 3 | não parou dentro da cena |
| 3c | o gateway parou de chamar o mock antes da recuperação | True | True | False |
| 3d | chamadas ao mock até o gateway parar de chamá-lo | 1 | 3 | não parou dentro da cena |
| 3d | o gateway parou de chamar o mock antes da recuperação | True | True | False |
| 4 | chamadas ao mock até o gateway parar de chamá-lo | 3 | 3 | não parou dentro da cena |
| 5a | 1ª requisição que deixou de chegar ao mock | não parou dentro da cena | 5 | não parou dentro da cena |
| 5b | 1ª requisição que deixou de chegar ao mock | não parou dentro da cena | 6 | não parou dentro da cena |
| 6 | o gateway parou de chamar o mock antes da recuperação | True | True | False |
| 6 | na janela da 1ª chamada lenta: (respondidas pelo mock, respondidas por outro) | [1, 7] | [8, 0] | [8, 0] |
| 6 | chamadas ao mock depois que ele voltou | 10 | 17 | 17 |

### Recursos e observabilidade

| Funcionalidade | Gateway próprio | LiteLLM | Como foi verificado |
|---|---|---|---|
| Fallback entre provedores na ordem configurada | sim (`chain`) | sim (`router_settings.fallbacks`) | experimento (cenas 3 a 9) |
| Motivo do fallback na resposta | header `x-gateway-fallback-reason` com o motivo por provedor, inclusive os pulados sem chamada | header `x-litellm-attempted-fallbacks` só com a contagem; o motivo não vem em header, e os headers que registrei (`x-litellm-model-id`, `-attempted-fallbacks`, ...) não aparecem nas respostas de erro; nos erros o motivo vem em texto livre no corpo | experimento (cenas 3, 7 e 8) |
| Corpo do erro quando tudo falha | 503 estruturado, com `error.providers` (motivo por provedor) e `Retry-After` | o status do erro do primeiro provedor da cadeia (429 com o mock em 429, 500 com o mock em 500, nos dois casos com a última falha sendo o erro de conexão do Ollama), uma mensagem única em texto livre que cita os grupos; sem `Retry-After` | experimento (cenas 7 e 7b, nesta versão e nesta configuração) |
| Limite local de requisições por minuto (rpm) | balde de 10 que reabastece 1 a cada 6 s; 10 chamadas chegaram ao mock | `rpm: 10` por minuto; 9 chamadas chegaram ao mock e as demais foram para o fallback, antes de chamar o mock | experimento (cena 2; o motivo de 9 e não 10 não foi investigado) |
| Respeito ao `Retry-After` de um 429 | sim: o cooldown dura o valor do header (2,0 a 2,2 s e 15,2 a 15,4 s) | com o contador ativo (`allowed_fails: 2`) o cooldown durou o valor do header (2,2 s e 15,3 a 15,4 s), não o `cooldown_time` de 10 s; no config versionado (`allowed_fails: 3`, contador inativo) um 429, ou mesmo 8 seguidos, não abriu cooldown | experimento (cenas 3a a 3d, nesta versão e nesta configuração) |
| Limite de falhas até parar de chamar o provedor | 3 falhas CONSECUTIVAS (um sucesso zera a contagem) | nesta versão e nesta configuração, contagem por janela de tempo que o sucesso não zera (com `allowed_fails: 2`, o cooldown abre quando as falhas passam desse valor); com o valor versionado (3, o padrão da biblioteca) o contador fica inativo | experimento (cenas 4, 5a e 5b; leitura de `router_utils/cooldown_handlers.py` confirma a janela) |
| Recuperação do provedor | meia-abertura com sonda controlada: uma chamada passa e as concorrentes vão ao fallback | nesta versão e nesta configuração, volta direto depois do cooldown (com `allowed_fails: 2`): todas as chamadas concorrentes foram ao mock | experimento (cena 6, com o mock lento) |
| Janela de contexto do provedor | pula o provedor antes de chamar, com estimativa de caracteres/4 | pula antes de chamar (`enable_pre_call_checks`), contando tokens com o tokenizador da biblioteca (9822 e 3574 tokens contra 11000 e 4000 da estimativa chars/4); responde 400, não 503 | experimento (cenas 8a e 8b; a diferença das estimativas só foi medida nestes dois prompts) |
| Forçar um provedor | header `x-gateway-force-provider`; sem fallback; nome desconhecido dá 400 | pedir o grupo pelo nome do modelo (`model: ollama_qwen`) funciona; para o grupo com fallbacks, `disable_fallbacks: true` no corpo desliga o fallback; não há header equivalente | experimento (cena 9) |
| Estado do gateway (status) | `GET /status` com breaker, baldes e cooldown restante por provedor | `/health/liveliness`, `/health/readiness`, `/health` (lista endpoints saudáveis), `/model/info`; sem endpoint com o estado de cooldown (`/cooldown` dá 404) | experimento |
| Métricas Prometheus | `GET /metrics` com 9 famílias (`gateway_requests_total`, `gateway_fallback_total`, `gateway_breaker_state`, `gateway_latency_seconds`, `gateway_budget_remaining`, ...) | não verificado com o callback `prometheus`: o pacote `prometheus_client` não está no lock; o endpoint dá 404 no modo só-config | experimento (o 404 foi observado; com o callback, não verificado) |
| Gravação de requisições e respostas em arquivo | recorder em JSONL (`record_content` liga ou desliga o conteúdo) | não encontrei logger de arquivo embutido; as integrações embutidas enviam a serviços remotos ou usam banco; um callback customizado em Python seria possível (não testado) | código instalado (busca por escrita em arquivo em `integrations/`, `litellm_core_utils/` e `proxy/`) |
| Orçamento ou contador de chamadas | orçamento de chamadas em arquivo (`data/budget.json`), reservado antes da chamada | `max_budget` do proxy é em custo, e o código diz que sem banco o orçamento NÃO é aplicado e nenhuma requisição é bloqueada | código instalado (aviso em `proxy_server.py`; não executado) |
| Limite de TPM e `timeout` por tentativa | TPM no balde local; `timeout_s` por tentativa | existem no config (`tpm`, `timeout`) | não verificado (a demo usa `tpm: null` e nenhuma cena mede o timeout) |
| Streaming | não suportado (400) | não avaliado | não verificado (fora do escopo da comparação; o LiteLLM é um produto muito mais amplo, não avaliado aqui) |
| Outros provedores de LLM | mock, Ollama e Anthropic (fail-closed) | não avaliado | não verificado (fora do escopo da comparação; o LiteLLM é um produto muito mais amplo, não avaliado aqui) |
| Chaves virtuais, UI e banco | não existe | não avaliado | não verificado (fora do escopo da comparação; o LiteLLM é um produto muito mais amplo, não avaliado aqui (só `/global/spend` e `/spend/logs` mostraram que dependem de banco)) |

### Trava de rede do LiteLLM (todas as subidas do proxy)

- Subidas do proxy: 66; processos Python com a trava armada: 66; amostras do monitor externo: 769.
- Eventos BLOQUEADOS: 0; conexões fora do loopback vistas pelo monitor externo: 0; processos restantes: 0.
- Eventos permitidos (todos para loopback):
  - 180 x `asyncio.sock_connect -> '127.0.0.1':mock`
  - 179 x `asyncio.sock_connect -> '127.0.0.1':ollama`
  - 42 x `asyncio.sock_connect -> '127.0.0.1':porta-sem-servico`
  - 133 x `socket.connect -> 127.0.0.1 (porta dinâmica, loopback do próprio proxy)`

<!-- END GENERATED: comparekit.report -->

CSV agregado: [`reports/fase2-gateway-vs-litellm.csv`](../reports/fase2-gateway-vs-litellm.csv) (uma linha por cena,
gateway e repetição).

## 6. Achados

**Do Marco 13**
- **CRLF do tiktoken:** os arquivos de cache embutidos no wheel têm CRLF, a verificação de SHA-256 do `tiktoken` os rejeita
  e o proxy tentou baixar o `cl100k_base` na inicialização; contornado com um cache temporário em LF conferido por hash
  (seção 2). Observado só neste ambiente e nesta versão.
- **A trava de rede funcionou:** bloqueou essa tentativa de saída antes de a conexão acontecer; no Marco 14, em todas as
  subidas do proxy, não houve nenhum bloqueio.

**Do Marco 14** (as 3 repetições concordaram em todas as linhas das tabelas)
1. **No LiteLLM 1.103.1, `failure_threshold: 3` corresponde a `allowed_fails: 2`, e o `allowed_fails: 3` do config versionado
   não reproduz o gateway próprio.** O cooldown abre quando as falhas **passam** de `allowed_fails`, e o código instalado
   (`router_utils/cooldown_handlers.py`, `_is_allowed_fails_set_on_router`) trata um `allowed_fails` igual ao padrão da
   biblioteca (3) como "não definido", caso em que vale outra regra (mais de 50% de falhas no minuto, com pelo menos 5
   requisições). Por isso, **com o config versionado**, 10 falhas 500 seguidas chegaram todas ao mock (cena 4) e 8 respostas
   429 seguidas também (cenas 3c e 3d): é efeito dessa configuração, não uma limitação do produto. Com `allowed_fails: 2`, o
   LiteLLM parou de chamar o mock depois de 3 falhas, como o gateway próprio. O mapa de equivalência do Marco 13
   (comentários de `litellm/config.litellm.yaml`) dizia que os dois valores 3 se equivaliam; foi corrigido e o teste que
   comparava os números agora confere que o mapa registra a diferença. Os valores do config não foram alterados.
2. **Falhas consecutivas x janela de tempo.** Nas falhas intercaladas (cenas 5a e 5b) o gateway próprio, que conta falhas
   consecutivas, não parou de chamar o mock (12 de 12 e 10 de 10). Nesta versão e nesta configuração, o LiteLLM com
   `allowed_fails: 2` parou depois da 3ª falha, mesmo com sucessos no meio (a 5ª requisição da 5a e a 6ª da 5b já não
   chegaram ao mock): o sucesso não zera a contagem. É coerente com a leitura do código (contador com TTL igual ao
   `cooldown_time`).
3. **`Retry-After`.** O gateway próprio espera o valor do header (2,0 a 2,2 s com `Retry-After` de 2 s; 15,2 a 15,4 s com
   15 s). No LiteLLM (config versionado), **um único 429 não abriu cooldown nenhum** (cenas 3a e 3b: a sondagem seguinte já
   voltou ao mock). Com o contador ativo (`allowed_fails: 2`) o cooldown abriu e durou o valor do header (2,2 s e 15,3 a 15,4 s), e não o
   `cooldown_time` fixo de 10 s. Não testado: 429 sem `Retry-After`.
4. **Recuperação.** O gateway próprio usa meia-abertura com sonda controlada: com o mock lento, na janela da primeira
   chamada 1 requisição chegou ao mock e 7 foram para o fallback. Nesta versão e nesta configuração, o LiteLLM (com
   `allowed_fails: 2`) voltou ao mock depois de 9,9 a 10,0 s (o `cooldown_time`) e deixou chegar todas as 8 requisições da
   janela: volta direto, sem sonda controlada.
5. **`rpm`.** Com o limite no mock em 10 por minuto e 13 requisições seguidas, o gateway próprio deixou 10 chegarem ao mock
   e o LiteLLM 9; nos dois o excedente foi para o Qwen sem chamar o mock. O motivo de 9 no LiteLLM não foi investigado.
6. **Tudo indisponível (cenas 7 e 7b).** O gateway próprio devolve 503 com `error.providers` (motivo por provedor) e os
   motivos também no header `x-gateway-fallback-reason`. O LiteLLM devolveu o status do erro do primeiro provedor da cadeia:
   **429 com o mock em 429 (cena 7) e 500 com o mock em 500 (cena 7b)**, nos dois casos com a última falha sendo o erro de
   conexão do Ollama; ou seja, nestes dois casos o status não seguiu a última falha. A mensagem é uma só, em texto livre, e cita
   os grupos e os erros; sem `Retry-After` e sem os headers `x-litellm-*` que o smoke tinha mostrado nas respostas 200. Só
   esses dois casos foram testados (3 repetições da 7b e da 7).
7. **Janela de contexto.** Os dois pulam o provedor antes de chamar (o mock só recebeu a requisição de controle da cena
   8a). O gateway próprio responde 503 com o motivo `context_too_large`; o LiteLLM responde 400
   (`ContextWindowExceededError`). **Por que o mock respondeu 429 na cena 8b:** a cadeia é `mock_market` (janela 8192),
   `ollama_qwen` e `ollama_llama` (3072 cada); o prompt (3574 tokens pela contagem do LiteLLM) cabe no mock, que por isso foi
   chamado, estava em 429 e falhou; o pré-filtro só pula os provedores cuja janela não comporta o prompt, ou seja, os dois
   Ollama. O erro final foi o do primeiro provedor (429), e a mensagem cita o erro de contexto de `ollama_llama`
   (`Got=3574`, máximo 3072); sobre `ollama_qwen` a mensagem não diz nada (não verificado se ele foi pulado ou como).
   No gateway próprio a ordem é a mesma: o mock foi chamado (429) e os dois Ollama foram pulados por `context_too_large`. As estimativas diferem: o prompt grande teve 9822 tokens contados pelo LiteLLM e 11000 pela estimativa
   caracteres/4 do gateway próprio (3574 e 4000 no prompt médio); só dois prompts foram medidos.
8. **Forçar provedor.** O gateway próprio tem `x-gateway-force-provider` (sem fallback; forçar o `mock_market` com o mock
   em 429 deu 503 sem fallback; nome desconhecido deu 400). O LiteLLM não tem header equivalente: pedir
   `model: ollama_qwen` funciona; pedir `model: mock_market` com o mock em 429 ainda faz o fallback (200 do Qwen); com
   `disable_fallbacks: true` no corpo o fallback não ocorre (429).
9. **Observabilidade.** Sem recurso enterprise e sem ligar nada externo: no modo só-config o LiteLLM não tem `/metrics`
   (404); com o callback `prometheus` ele não sobe neste ambiente porque o pacote `prometheus_client` não está no lock com
   hashes (não foi instalado nada novo). Não há endpoint com o estado de cooldown (`/cooldown` dá 404). O gateway próprio
   expõe `/status` e `/metrics`.

## 7. Fora do escopo da comparação

Streaming, outros provedores de LLM, chaves virtuais, UI e banco: **o LiteLLM é um produto muito mais amplo, e isso não
foi avaliado aqui.** A tabela de recursos marca essas linhas como "não verificado". O gateway próprio é um projeto de
estudo e não tem essas funcionalidades.

## 8. Limitações

- Vale para o **LiteLLM 1.103.1 (setembro/2026), em um Windows 11, no modo só-config, num cenário específico com o mock**;
  não deve ser generalizado, e versões diferentes podem se comportar de modo diferente.
- É uma comparação de **comportamento num cenário**, não de qualidade geral nem de desempenho. Latências (que ficam só nos
  dados brutos) de uma máquina, sem concorrência e com 3 repetições são só indicativas.
- **O config versionado mantém `allowed_fails: 3`, que NÃO reproduz o comportamento do gateway próprio** (o contador fica
  inativo); a variante `allowed_fails: 2` é a comparável, e só foi executada nas cenas 3c, 3d, 4, 5a, 5b e 6. Nas demais
  cenas só o config versionado rodou.
- O config do LiteLLM não é o único possível: o resultado depende de `allowed_fails`, `cooldown_time`, `rpm` e da
  estratégia de roteamento escolhidos; as diferenças entre as duas colunas do LiteLLM mostram isso. Outras combinações não foram testadas.
- Resolução do tempo: 0,25 a 0,5 s. As três repetições concordaram em tudo, mas é uma amostra pequena e determinística
  (mock).
- Não verificado: TPM, `timeout` por tentativa, 429 sem `Retry-After`, o motivo do 9 no `rpm`, quais métricas o callback
  `prometheus` exporia, gravação em arquivo com callback customizado, comportamento com vários workers ou Redis.
- Os achados de código instalado (gravação em arquivo, orçamento sem banco) vêm de leitura e busca, não de execução.
