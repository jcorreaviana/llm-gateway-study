# STATUS

## Estado atual
- **Marco 1 (setup e segurança): concluído e commitado** (sem push).
  `.gitignore`, `.env.example`, LICENSE MIT, `requirements.txt`, `pytest.ini`,
  `config.yaml` inicial, README esqueleto, `scripts/check_secrets.py` (+ 4 testes passando).
- **Marco 2 (mock upstream): concluído e commitado** (sem push).
  `mock_upstream/server.py` (`create_app()`, bind fixo em 127.0.0.1, porta em
  `mock_upstream.port` do `config.yaml`, padrão 9001), 17 testes em `tests/test_mock_upstream.py`,
  README com a API de chaos. Suíte completa: 21 testes passando.
- **Marco 3 (gateway e roteamento): concluído e commitado** (sem push).
  `gateway/config.py` (pydantic), `providers.py`, `router.py`, `main.py`; 28 testes novos
  (`test_gateway_config.py`, `test_gateway_routing.py`). Suíte completa: 49 testes passando.
  Smoke test manual OK (dois processos + Ollama real): mock `normal` -> `mock_market`;
  `rate_limit` -> `ollama_qwen` com `x-gateway-fallback-reason: mock_market:rate_limited`;
  `/stats` do mock: 2 recebidas (200:1, 429:1). Log do gateway sem prompt.
- **Marco 4 (resiliência): concluído e commitado** (sem push).
  `gateway/ratelimit.py`, `breaker.py`, `availability.py` (cooldown + guard por provedor),
  `/status`, `warmup` opcional. 60 testes novos (`test_ratelimit.py`, `test_breaker.py`,
  `test_resilience.py`). Suíte completa: 109 testes passando.
  Smoke manual OK (processos reais): mock em `server_error` -> breaker abriu após 3 falhas
  consecutivas; `/status` = `open` e `/stats` do mock parou em 3 recebidas mesmo com 5 requisições
  (`mock_market:breaker_open`); mock em `normal` + cooldown de 30 s -> `half_open` -> sonda 200 ->
  `closed`. `ollama ps` logo após resposta do Qwen: `qwen2.5:3b`, 2.2 GB, **100% GPU**, contexto 4096.
  Log do gateway sem prompt. Processos encerrados.
- **Marco 5 (custo, métricas e recorder): concluído e commitado** (sem push).
  `gateway/budget.py`, `secrets.py`, `metrics.py`, `recorder.py`, `scripts/reset_budget.py`;
  `AnthropicProvider` liberado atrás do guard; `GET /metrics`; `/status` com budget. 93 testes novos
  (`test_budget.py`, `test_anthropic.py`, `test_metrics_recorder.py`, `gw_helpers.py`, `conftest.py`).
  Suíte completa: 202 passando. **Nenhuma chamada real à Anthropic** (só `httpx.MockTransport`).
  Smoke manual OK (processos reais): mock `normal` -> `mock_market`; `rate_limit` -> `ollama_qwen`;
  `/metrics` com `gateway_fallback_total{from=mock_market,to=ollama_qwen,reason=rate_limited}`;
  tentativa forçada no `anthropic` com `ENABLE_REAL_API=0` -> 503 `real_api_blocked`, budget
  inalterado (`used=0`) e breaker `closed`; `git check-ignore` confirma `logs/` e `data/`. Processos
  encerrados, `git status` limpo.
- **Marco 6 (consolidação da suíte): concluído e commitado** (sem push).
  `docs/TEST_MATRIX.md` (requisito -> teste), `pytest-cov` (97%, `fail_under` 85), `pytest-randomly`,
  `ruff`, bloqueio de rede (`tests/netguard.py`), canário de segredo ponta a ponta, 5 testes de
  integração (`-m integration`, Ollama real + processos reais), CI em `.github/workflows/ci.yml`.
  Suíte padrão: 253 passando + 5 de integração (também passando). 5 execuções aleatórias
  (sementes 101/202/303/404/505): todas verdes. 6 quebras intencionais: todas detectadas.
  `data/` e `logs/` do repo idênticos antes e depois (hash/mtime). **Nenhuma chamada real à Anthropic.**
- **Marco 7 (demo e README): concluído e commitado** (sem push).
  `config.demo.yaml`, `scripts/demo.py` (6 cenas, processos reais), `tests/test_demo.py` (23 testes),
  README reescrito. Suíte padrão: 277 passando + 5 de integração; cobertura 97%; ruff limpo.
  Demo real rodada uma vez (22 requisições, 13 fallbacks, exit 0): sem processo órfão, `git status`
  limpo e `data/`/`logs/` do repo idênticos (hash/mtime). **Nenhuma chamada real à Anthropic.**
  README verificado em clone limpo (venv, pip, demo, pytest, cov, ruff, integração, mock+gateway à mão,
  `curl.exe`, `GATEWAY_CONFIG`, check_secrets, install_hooks, reset_budget).
- **Marco 8 (varredura final e push): concluído. Fase 1 concluída e publicada.**
  Repositório: https://github.com/jcorreaviana/llm-gateway-study (branch `main`, 49 arquivos).
  Antes do push: diagramas Mermaid (arquitetura e sequência com 429) no README, validados com o
  Mermaid CLI; CI em matriz `ubuntu-latest` + `windows-latest`. Varredura: `check_secrets.py` (arquivos e
  histórico) limpo, sem dado pessoal em arquivos, patches, autores ou mensagens, só o e-mail noreply, sem
  `secrets.*` no CI. O canário de teste só se forma em runtime (nenhuma string contínua com formato de
  chave no histórico). Push sem `--force`, aceito sem rejeição. **CI do primeiro run: verde nas duas
  plataformas** (ubuntu-latest e windows-latest; os dois ramos do lock do budget, `fcntl` e `msvcrt`,
  agora exercitados). Anotações do GitHub, sem falha: Node.js 20 deprecado nas actions (`checkout@v4`,
  `setup-python@v5`) e `ubuntu-latest` migra para Ubuntu 26 em 19/10/2026.

## Decisões
- Python 3.12, venv em `.venv/`. Ollama com `qwen2.5:3b` e `llama3.2:3b` já instalados.
- Remote `origin` configurado com URL limpa (sem credenciais). Credential helper `manager`
  (config de sistema do Git for Windows) confirmado.
- Identidade de commit definida só localmente (`git config --local`) com o e-mail noreply do
  GitHub. A config global da máquina não foi alterada. Conferir sempre
  `git log --format="%h %an <%ae>"` antes do push.
- `check_secrets.py` exclui a si mesmo; testes montam chaves falsas em runtime.
  `--history` varre `git log -p --all` (rodar antes do push). Histórico atual: limpo.
- `AnthropicProvider` fail-closed desde o Marco 3 (recusa chamada real até o budget existir).
- Budget (Marco 5): reservar a chamada ANTES de enviar; sem `fcntl` (Windows): lock via
  `msvcrt`/arquivo + `os.replace`.
- Push só no Marco 8: `git push -u origin main`, sem `--force`, após confirmação, precedido de
  varredura completa (arquivos + histórico), `git status` e lista de arquivos.
- Commits: Conventional Commits, pequenos e frequentes.
- Mock (Marco 2): `POST /chaos` substitui a config inteira (omitidos voltam ao padrão) e zera o
  contador do `fail_every_n`; `fail_every_n` conta só requisições ao chat e vale em qualquer modo.
  `error_status` aceita só 500/503; `fail_status` 400-599 (429 inclui `Retry-After`). Campos
  extras no `/chaos` dão 422. `/stats` conta só o chat; extra não pedido: `POST /stats/reset`.
- `pytest.ini` ganhou `pythonpath = .` para os testes importarem `mock_upstream`.

- Gateway (Marco 3):
  - `config.yaml` mudou: `providers` agora é lista; `gateway.allow_force_provider` virou
    `gateway.force_provider_enabled`; `gateway.port` (padrão 8000, bind fixo 127.0.0.1).
    Campos desconhecidos no YAML (ex.: `api_key`) são rejeitados.
  - Falha tipada (`FailureReason`) além da spec: `invalid_response` (2xx sem JSON / 3xx, com
    fallback), `disabled` (provedor desabilitado na chain aparece no header de fallback) e
    `client_error` (4xx, sem fallback, corpo repassado como veio com `x-gateway-provider`).
  - `Retry-After` já é parseado (segundos ou data HTTP) em `ProviderFailure.retry_after_s`.
  - Ponto de extensão do Marco 4: `Router.check_available(provider)` (devolve motivo ou None),
    chamado depois de `disabled`/`context_too_large` e antes de cada tentativa.
  - Header `x-gateway-force-provider` com nome desconhecido (e flag ligada) -> 400.
  - Um único `httpx.AsyncClient`, com `timeout_s` por requisição; `create_app(config, client=None)`
    aceita client injetado (testes usam `mounts` por porta: ASGI para o mock, `MockTransport`
    para o Ollama). Uvicorn: `python -m gateway.main` ou `gateway.main:app_factory --factory`.
  - Estimativa de contexto: caracteres/4 (aproximação documentada).
  - Smoke: o "OlÃ¡" visto no PowerShell foi só decodificação do console, não do gateway.
  - Falso positivo do `check_secrets` em teste (`api_key=` literal) contornado montando o nome
    do campo em runtime.

- Resiliência (Marco 4):
  - `scripts/install_hooks.py` instala o pre-commit (roda `check_secrets.py`; bloqueia o commit se
    falhar). Hook instalado neste clone; reinstalar em cada clone. Nunca encadear scan e commit
    sem `&&`.
  - `Router.check_available(provider, est_tokens)` agora devolve `Availability` (motivo,
    `retry_after_s`, admissão do breaker), não só o motivo. Ordem: desabilitado -> contexto ->
    cooldown -> breaker -> bucket -> chamada. `Router` e `create_app` aceitam `now` (relógio
    injetável); `app.state.router` fica exposto para testes.
  - Admissão do breaker carrega uma "geração": resultado de chamada iniciada antes de uma
    transição é ignorado (evita confundir chamada lenta de `closed` com sonda). Vaga de sonda é
    devolvida (`release`) em 429, erro do cliente, `real_api_blocked`, cancelamento, ou se o
    bucket negar depois da admissão do breaker.
  - `real_api_blocked` NÃO conta para o breaker (é bloqueio local fail-closed, não saúde do provedor).
  - Bucket: começa cheio (burst = rpm); pedido maior que o balde passa uma vez com o balde cheio;
    falha não devolve o consumo; `usage.total_tokens` pode deixar o TPM negativo (dívida).
  - 429 com `Retry-After: 0` não gera cooldown; sem header usa `default_retry_after_s` (padrão 30).
  - 503 de indisponibilidade: `Retry-After` = menor espera (inteiro, teto) entre os que informaram
    (cooldown, breaker aberto, bucket local, 429 recém-recebido); sondas ocupadas não informam.
  - Prazo total por tentativa via `asyncio.timeout(timeout_s)` em volta do httpx (que só limita
    por fase). Timeout cancela de verdade e conta para o breaker.
  - `warmup` só para Ollama (validado), roda no lifespan, fora do breaker, falha só avisa. Não
    foi exercitado no smoke (padrão desligado); coberto por teste.
  - `config.yaml`: `mock_market` com `rpm: 5` (demo). Em uso real, 5 rpm esgota rápido: ajustar.

- Custo e observabilidade (Marco 5):
  - `config.yaml`: `context_window` dos Ollama = 3072 (contexto carregado é 4096; a janela cobre
    prompt + resposta e a estimativa chars/4 é aproximada). Novas seções `budget` (`limit` 30,
    `path`) e `recorder` (`path`, `record_content`); no provedor anthropic, `real_api_max_input_tokens`
    (1000) e `max_tokens_cap` (50). O `anthropic` perdeu o `base_url` (host fixo no código).
    `.env.example` perdeu `ANTHROPIC_MODEL`/`ANTHROPIC_MAX_TOKENS`/`REAL_API_CALL_LIMIT` (agora no YAML).
  - Ordem das checagens do AnthropicProvider: flag -> chave -> enabled -> tamanho -> reserva. A reserva
    é a ÚLTIMA, para um bloqueio não gastar orçamento. `budget_exhausted` e `input_too_large` ficam fora
    de `BREAKER_FAILURES` (vaga de sonda devolvida). `NOT_CALLED` (providers.py) = motivos locais, sem
    conexão e fora do histograma de latência.
  - Budget: `{"used": n}` em `data/budget.json` (o limite vem do config). Lock em arquivo `.lock`
    (`msvcrt`/`fcntl`) + `threading.Lock`, escrita via arquivo temporário + `os.replace` (limpa o
    temporário se falhar). `.gitignore` agora ignora `data/` inteiro (lock e temporários). `status()`
    só lê; `ensure()` (lifespan) cria em zero. Arquivo inválido: `reserve()` -> False e `status()`
    traz `error`; nunca reinicia. Sem dependência nova (nem portalocker).
  - Caminhos relativos do config (budget, recorder) valem a partir da RAIZ do projeto
    (`gateway.config.PROJECT_ROOT`), não do cwd; `tests/conftest.py` redireciona para `tmp_path` e
    limpa `ENABLE_REAL_API`/`ANTHROPIC_API_KEY` em todo teste.
  - `request_id` agora é uuid4 completo (antes 12 hex), igual nos logs, no header
    `x-gateway-request-id` (todas as respostas do chat) e no recorder.
  - Métricas: registry próprio por app; `gateway_requests_total` é UMA por requisição (`provider` =
    quem respondeu, `none` no 503 e nos 400 do gateway); fallback conta cada provedor pulado/falho com
    `to_provider` = quem respondeu (`none` no 503); breaker e budget são coletados no scrape.
    `real_api_enabled` no `/status` é só `ENABLE_REAL_API=1` (não olha chave nem `enabled`).
  - Recorder: `response` = texto do assistente; falhas gravam `response: null`. Escrita síncrona com
    `threading.Lock` (um processo); falha só avisa. Máscara: padrão `sk-...` + valor atual da chave.
  - Smoke: com `anthropic.enabled: false` o router pula por `disabled` antes do provedor; para ver
    `real_api_blocked` foi preciso ligar `enabled` e `force_provider_enabled` temporariamente no YAML
    (revertido; nada commitado).
  - Teste de tempo do mock (`test_slow_respects_latency`) falhou uma vez de forma intermitente
    (margem de relógio no Windows); corrigido no Marco 6.

- Suíte de testes (Marco 6):
  - `test_slow_respects_latency` agora mede o PEDIDO de espera: `mock_upstream.create_app(sleep=...)`
    aceita um sleep injetável. Sobrou um teste de relógio real só com limite inferior folgado (>= metade).
  - Isolamento: `tests/conftest.py` (autouse) redireciona `PROJECT_ROOT` para `tmp_path`, limpa
    `ENABLE_REAL_API`/`ANTHROPIC_API_KEY`, bloqueia rede externa e, em escopo de sessão, falha se algo
    mudar em `data/` ou `logs/` do repo. A exceção do guard NÃO herda de `OSError` (o código sob teste
    não a engole como `connection_error`) e as violações também falham o teste no fim. No Windows o
    asyncio (Proactor) não passa por `socket.connect`, então o guard também cobre `getaddrinfo`,
    `create_connection` e `sock_connect`.
  - Pontos de entrada ganharam dois ganchos por ambiente, só para os testes de integração:
    `MOCK_UPSTREAM_PORT` (mock) e `GATEWAY_CONFIG` (`app_factory` do gateway). Nada muda sem eles.
  - Integração: um stack (mock + gateway) NOVO por teste. Com stack compartilhado, o cooldown de um
    429 vazou para o teste seguinte (dependência de ordem); cooldown/breaker/budget são estado de processo.
  - Ruff: `E,F,W,I,B`, linha 120, sem `E501` em `tests/`. `UP` ficou de fora (UP042 trocaria
    `str, Enum` por `StrEnum`, mudando `str()`/format dos enums de motivos). Só 7 correções reais
    (imports não usados/ordem); nada reformatado.
  - Quebras intencionais (um teste falha em cada): trava `ENABLE_REAL_API` (4), budget sem incrementar
    (30), máscara de segredo (5), cooldown removido (5) ou ignorando `Retry-After` (3), `force_provider_enabled`
    ignorado (1). A quebra 1 revelou um teste fraco (forçar `anthropic` sem chave bloqueava pelo motivo
    errado); reforçado com a chave presente.
  - CI: não foi executado (só vale após o push do Marco 8); roda em Linux, onde o ramo `fcntl` do
    budget e os testes de processo nunca rodaram. Pode exigir ajuste no primeiro run.

- Demo e README (Marco 7):
  - `config.yaml` perdeu o `rpm: 5` do `mock_market` (sem limites artificiais; a Fase 3 usa este). Os
    limites da demo estão em `config.demo.yaml` (`rpm` 10, breaker `cooldown_s` 10, `warmup` nos Ollama,
    `force_provider_enabled` true). Os dois passam no mesmo schema (teste carrega ambos).
  - A demo deriva uma cópia temporária do `config.demo.yaml` (portas livres, `base_url` do mock, budget e
    recorder no diretório temporário, e `cooldown_s` 2 com `--fast`), validada pelo schema, e passa o
    caminho em `GATEWAY_CONFIG`. Nada em `data/` nem `logs/`.
  - Ordem das cenas: Normal, Rate limit, Falha, Recuperação, **Limite local**, Tudo indisponível. O
    "Limite local" ficou depois porque esvazia o balde do mock (reabastece 1 a cada 6 s) e as cenas 2-4
    precisam chamar o mock. `rpm` 5 não serve: as cenas 1-4 usam ~8 chamadas ao mock.
  - "Tudo indisponível" força um único provedor (`mock_market` com balde vazio -> `rate_limit_local`, e
    `anthropic` -> `disabled`): derrubar os Ollama de propósito não foi viável. O 503 mostra
    `error.providers` e `Retry-After`.
  - Ambiente dos filhos: `ENABLE_REAL_API=0` e `ANTHROPIC_API_KEY` **vazia** (não ausente): ausente deixaria
    o `load_dotenv` do gateway ler a chave de um `.env` local; variável já definida não é sobrescrita.
  - `Demo` recebe clientes httpx, `sleep` e `perf` injetáveis (testes rodam as cenas em memória com
    relógio falso). A tabela é ASCII puro; não-ASCII da resposta vira `?`. `scripts/` não entra na
    cobertura (a fonte é `gateway` e `mock_upstream`).
  - O `Stack` da demo duplica a lógica de processos de `tests/test_integration.py` (não refatorei os testes
    de integração). No Windows os filhos sobem com `CREATE_NEW_PROCESS_GROUP` (Ctrl+C só no pai).
  - README: `curl.exe` com JSON direto em `-d` quebra no PowerShell 5.1 (espaços/aspas); o exemplo usa
    arquivo (`-d "@arquivo"`). `Activate.ps1` bloqueado por política não foi testado (só orientação).

## Como rodar os testes
`.venv\Scripts\python -m pytest`  (aleatório; `--cov` para cobertura; `-m integration` com Ollama real;
`ruff check .` para lint). Detalhes no README.

## Dívidas conhecidas
- A lógica de processos (subir/encerrar mock e gateway) está duplicada entre `scripts/demo.py` (`Stack`)
  e `tests/test_integration.py`.
- Bucket: `usage.total_tokens` pode deixar o TPM negativo.
- Estado de cooldown, breaker, buckets e métricas só em memória; recorder e budget assumem um processo.
- Estimativa de tokens aproximada (caracteres/4); sem streaming.
- Actions do CI em versões que rodam em Node 20 (aviso de deprecação); revisar versões.
- Validação real única da Anthropic (1 chamada, `max_tokens` 20) nunca feita: só com autorização explícita.

## Próximos passos
1. **Fase 2:** comparar com o LiteLLM Proxy (mesmo cenário de fallback, rate limit e cooldown).
2. **Fase 3:** avaliação de qualidade das respostas locais contra um baseline, usando os registros do
   recorder (`logs/requests.jsonl`).
