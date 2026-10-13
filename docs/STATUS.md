# STATUS

## Estado atual
- **Marco 1 (setup e segurança): concluído e commitado** (sem push).
  `.gitignore`, `.env.example`, LICENSE MIT, `requirements.txt`, `pytest.ini`,
  `config.yaml` inicial, README esqueleto, `scripts/check_secrets.py` (+ 4 testes passando).
- **Marco 2 (mock upstream): concluído e commitado** (sem push).
  `mock_upstream/server.py` (`create_app()`, bind fixo em 127.0.0.1, porta em
  `mock_upstream.port` do `config.yaml`, padrão 9001), 17 testes em `tests/test_mock_upstream.py`,
  README com a API de chaos. Suíte completa: 21 testes passando.
- Marcos 3-8: pendentes.

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

## Como rodar os testes
`.venv\Scripts\python -m pytest`  (testes de integração com Ollama: `-m integration`)

## Próximo passo exato
Marco 3: gateway e roteamento. Interface de provider, `MockProvider` (aponta para o
`mock_upstream`), `OllamaProvider`, `AnthropicProvider` fail-closed, cadeia de fallback
lida do `config.yaml` e gatilhos iniciais (429, 5xx/timeout). Testes contra o mock via ASGI.
