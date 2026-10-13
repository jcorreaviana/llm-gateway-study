# STATUS

## Estado atual
- **Marco 1 (setup e segurança): concluído.** `.gitignore`, `.env.example`, LICENSE MIT,
  `requirements.txt`, `pytest.ini`, `config.yaml` inicial, README esqueleto,
  `scripts/check_secrets.py` (+ testes).
- Marcos 2-8: pendentes.

## Decisões
- Python 3.12, venv em `.venv/`. Ollama com `qwen2.5:3b` e `llama3.2:3b` já instalados.
- `check_secrets.py` exclui a si mesmo; testes montam chaves falsas em runtime.
  `--history` varre `git log -p --all` (rodar antes do push).
- `AnthropicProvider` fail-closed desde o Marco 3 (recusa chamada real até o budget existir).
- Budget (Marco 5): reservar a chamada ANTES de enviar; sem `fcntl` (Windows): lock via
  `msvcrt`/arquivo + `os.replace`.
- Commits com identidade local (`git config --local`) usando e-mail noreply do GitHub.
- Push só no Marco 8, `git push -u origin main`, sem `--force`, após confirmação.

## Como rodar os testes
`.venv\Scripts\python -m pytest`

## Próximo passo exato
Marco 2: `mock_upstream/server.py` (`/v1/chat/completions` + `POST /chaos`).
