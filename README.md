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

*(As seções de chaos, métricas e demo serão adicionadas nos próximos marcos.)*
