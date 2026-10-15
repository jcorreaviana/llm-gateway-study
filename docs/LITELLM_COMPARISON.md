# Fase 2: comparação com o LiteLLM Proxy

Estado: **Marco 13 concluído** (instalação isolada, config equivalente e smoke). O Marco 14 verifica o
comportamento por experimento. **Nenhuma chamada à Anthropic** em nenhum processo (`ANTHROPIC_API_KEY` vazia,
nenhum provedor Anthropic no config).

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

## 4. O que NÃO foi verificado (Marco 14)

O mapa no topo de `litellm/config.litellm.yaml` marca com `[VERIFICAR]`: respeito ao `Retry-After` de um 429 e o
cooldown resultante; se o breaker (`allowed_fails`) conta falhas consecutivas ou numa janela; se um 429 conta para
ele; checagem de janela de contexto e com que estimativa; limite de TPM; se o `rpm` é aplicado de fato e o que acontece
no excedente; ponto para forçar um provedor; sonda de recuperação (`half_open`); formato do erro quando tudo falha;
endpoints de status e métricas; se `timeout` é por tentativa.
