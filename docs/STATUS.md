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
- Marcos 5-8: pendentes.

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

## Como rodar os testes
`.venv\Scripts\python -m pytest`  (testes de integração com Ollama: `-m integration`)

## Próximo passo exato
Marco 5: custo, métricas e recorder. Budget com reserva ANTES da chamada (lock via arquivo +
`os.replace`, sem `fcntl`) para liberar o `AnthropicProvider`; métricas Prometheus (incl. estados
do breaker e motivos de fallback); recorder de requisições sem prompt/segredos.
