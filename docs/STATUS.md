# STATUS

## Estado atual
- **Marco 1 (setup e segurança): concluído e commitado** (sem push).
  `.gitignore`, `.env.example`, LICENSE MIT, `requirements.txt`, `pytest.ini`,
  `config.yaml` inicial, README esqueleto, `scripts/check_secrets.py` (+ 4 testes passando).
- Marcos 2-8: pendentes.

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

## Como rodar os testes
`.venv\Scripts\python -m pytest`  (testes de integração com Ollama: `-m integration`)

## Próximo passo exato
Marco 2: `mock_upstream/server.py` (FastAPI) com `/v1/chat/completions` e `POST /chaos`
(`mode`: normal | rate_limit com `Retry-After` | server_error | slow com latência em ms;
opcional: falhar a cada N requisições), mais testes.
