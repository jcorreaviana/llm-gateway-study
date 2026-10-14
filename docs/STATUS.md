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
- **Correção pós-push (14/10/2026): concluída.** O histórico foi reescrito UMA vez (48 commits) só para remover
  o trailer `Co-Authored-By` das mensagens; árvore (hash de tree idêntico), autor, committer e datas intactos.
  Push único com `--force-with-lease` (remoto só com `main`, 0 forks, 0 PRs, 0 estrelas). `LICENSE` agora tem
  o titular `José Corrêa Viana`, 2026 (commit `docs: set license holder`); o scanner não tem regra de nome,
  então nenhuma exceção foi aberta. Verificação: `check_secrets.py` (arquivos e `--history`) limpo, 277 testes,
  cobertura 97,33%, ruff limpo. **CI do novo HEAD (`6ccc8ed`): verde em ubuntu-latest e windows-latest.**
  Backup local e refs do filter-branch removidos.

- **Marco 9 (Fase 3: CI, dataset e runner): concluído e publicado** (push normal, sem `--force`, de `2ba8187` a
  `e635fbf`; antes do push: autor só noreply, 0 trailers `Co-Authored-By`, `git ls-files` com o dataset e sem
  `eval/results/` nem `eval/baseline/`, `check_secrets.py` limpo em arquivos e `--history`). **CI do HEAD
  `e635fbf`: verde em windows-latest e ubuntu-24.04** (checkout@v7 e setup-python@v7, todos os passos).
  - **CI atualizado e verde:** `ubuntu-24.04` fixo (no lugar de `ubuntu-latest`), `actions/checkout@v7` e
    `actions/setup-python@v7` (ambas `using: node24`, confirmado no `action.yml` das tags; releases mais recentes
    v7.0.1 e v7.0.0). Run do push `2ba8187`: verde em ubuntu-24.04 e windows-latest, sem aviso de Node 20.
    `windows-latest`: nenhum aviso de mudança no `actions/runner-images` (anúncios consultados em 14/10/2026), então
    ficou como está. O anúncio do Ubuntu 26 diz que a migração do `ubuntu-latest` começa em 19/10/2026 e termina
    em 19/11/2026 (não é um corte único em 19/10).
  - **Dívida da lógica de processos: resolvida.** `scripts/_stack.py` (`start_stack`, `child_env`, `free_ports`,
    `wait_health`, `stop`, `Stack`, `StackError`) é usado pela demo, pelos testes de integração e pelo runner.
    Demo `--fast` rodada de novo (exit 0, sem processo órfão); 277 testes + 5 de integração seguiram verdes.
  - `config.eval.yaml`: chain só `ollama_qwen` e `ollama_llama`, `anthropic` desabilitado e fora da chain,
    `force_provider_enabled`, sem rate limit, breaker `failure_threshold` 1000, `timeout_s` 120, `warmup`,
    `context_window` 3072, `record_content` true.
  - Dataset sintético em PT-BR: `eval/prompts/*.jsonl`, 6 categorias x 8 = 48 prompts (versionado). Schema em
    `evalkit/schema.py` e validador `python -m evalkit.validate_dataset`.
  - Runner `python -m evalkit.run --provider <p> --run-id <id> [--resume] [--limit N]`, um provedor por vez, em
    sequência, provedor forçado, `temperature` 0; saída em `eval/results/<run_id>/` (ignorado).
  - Testes: 373 passando + 7 de integração (2 novos, parametrizados, do runner); cobertura 96,6% (agora inclui
    `evalkit/`); ruff limpo.
  - **Smoke real (`--limit 3`, run `smoke-m9`, Ollama 0.40.3):** Qwen 3 ok / 0 erros / p50 68 ms; Llama 3 ok /
    0 erros / p50 58 ms (modelos já carregados). `eval/results/` fora do `git status`; `data/` e `logs/` do
    repositório idênticos antes e depois; nenhum processo órfão. **Nenhuma chamada à Anthropic.** Os 48 prompts NÃO
    foram rodados (ficam para quando os scorers existirem).

- **Marco 10 (Fase 3: scorers, similaridade e estatística): concluído e publicado** (push normal, sem `--force`, de
  `151b8ec` a `3852a68`; autor só noreply, 0 trailers `Co-Authored-By`, `eval/results/` fora do `git ls-files`,
  `check_secrets.py` limpo em arquivos e `--history`). **CI do HEAD `3852a68`: verde em windows-latest e ubuntu-24.04**
  (o CI não instala `requirements-eval.txt`; o teste do modelo real segue só como integração).
  `evalkit/scorers.py` (6 tipos de `check` + summarization composta), `embedding.py` (`Embedder` injetável, modelo
  real opcional), `stats.py` (Wilson, percentis, bootstrap com semente, McNemar exato), `score.py`
  (`python -m evalkit.score --run-id <id> [--review-sample] [--no-embedding | --allow-download]`),
  `requirements-eval.txt`. Testes: 514 passando + 8 de integração (1 novo, o modelo real); cobertura 96,0%; ruff
  limpo; `check_secrets.py` limpo (arquivos e `--history`). 4 quebras intencionais (`numeric` e `label_in`) todas
  detectadas. **Nenhuma chamada à Anthropic.**
  - **Execução completa:** run `m10-full`, 48 prompts por provedor, 0 erros nos dois (Qwen p50 212 ms, Llama 230 ms).
    Resultado (aprovação, com embedding): Qwen 88% (IC 75-94%), Llama 65% (IC 50-77%). Pareado: dif. média de score
    0,15 (IC95 0,06-0,26), McNemar 12 x 1, p=0,0034 -> diferença detectada a favor do Qwen no conjunto de 48;
    **por categoria nada é conclusivo** (8 pares cada). Nenhum prompt defeituoso: os 5 em que os dois reprovam
    (extraction-06, instruction-08, reasoning-03/05/08) são erros reais dos modelos (conferido à mão). `reasoning` é a
    categoria mais fraca (Qwen 50%, Llama 38%), em parte porque o enunciado proíbe explicar (sem cadeia de raciocínio).
  - **Correção antes do push (`fix: pin embedding revision and force cpu`):** o embedding agora roda em `device="cpu"` e
    a revision do modelo é fixada no código (`DEFAULT_REVISION` `e8f8c211...`, o commit já em cache; outro modelo não
    herda essa revision e aparece como `unpinned`). O `meta.json` do run ganhou a seção `scoring` (data, semente,
    status do embedding, modelo, revision, device, `max_seq_length`), também em `summary.json`. Texto acima de 128
    tokens (referência ou resposta) é registrado em `details.embedding_truncated`: no `m10-full` só 1 de 16 resumos
    passou (llama summarization-02, resposta); os scores não mudaram. Totais finais: 519 testes + 8 de integração,
    cobertura 96,15%.
  - A revisão humana de amostra achou um defeito do scorer (frase de abertura contada como passo na summarization),
    corrigido e coberto por teste antes dos commits. O dataset NÃO foi alterado (hash `c64fa1474d38`).
  - O modelo de embedding (`paraphrase-multilingual-MiniLM-L12-v2`, 458 MB) JÁ estava no cache local do Hugging Face
    (desde 10/08/2026); nada foi baixado agora. Só o `pip install -r requirements-eval.txt` instalou
    `sentence-transformers` e `torch` na `.venv`. A `Qwen2.5-0.5B-Instruct` no mesmo cache (954 MB) é anterior e
    não tem relação com este projeto.

- **Marco 11 (Fase 3: juiz local, calibração e revisão humana): concluído e publicado** (push normal, sem `--force`, de
  `520fec0` a `6f8f637`; autor só noreply, 0 trailers `Co-Authored-By`, `eval/results/` fora do `git ls-files`,
  `check_secrets.py` limpo em arquivos e `--history`). **CI do HEAD `6f8f637`: verde em windows-latest, VERMELHO em
  ubuntu-24.04.** Job `test (ubuntu-24.04)`, passo "Testes com cobertura (sem integração, sem segredos)": falha só
  `tests/test_judge.py::test_execute_runs_every_judgment_and_records_meta` (linha 493), que compara
  `[p.name for p in run_dir.glob("*.jsonl")]` com `[llama, qwen]`; no Linux o `glob` não vem ordenado (veio qwen antes de
  llama). É defeito do TESTE (falta `sorted`), não do código; no Windows a ordem coincidiu. CORRIGIDO no Marco 12 (`sorted`; commit
  `96dd228`); o CI do HEAD `0608afc` ficou verde nas duas plataformas.
  `evalkit/judge_prompts.py` (rubricas por `task_type`, prompts PT-BR, hash), `judge.py` (pontual + pareado com troca de
  ordem, parser tolerante, uma nova tentativa, `judge_error`, `--resume`), `judge_stats.py` (kappa, acurácia, Spearman,
  viés de posição) e `judge_report.py` (`judge_summary.json` + `judge_review.md`). Testes: 653 passando + 10 de integração
  (2 novos, um por juiz); cobertura 96,32%; ruff limpo; `check_secrets.py` limpo (arquivos e `--history`). 5 quebras
  intencionais, todas detectadas (troca de ordem: 2 testes; filtro do `check`: 2; injeção: 3; nova tentativa: 4; limiar: 2).
  **Nenhuma chamada à Anthropic** (budget do gateway em `used: 0` nos dois juízes). Dataset intacto (`c64fa1474d38`).
  - **Execução:** run `m10-full`, cada juiz com 192 julgamentos (96 pontuais + 96 pareados), 0 `judge_error`, 0 novas tentativas.
    Um juiz por vez (Qwen, depois Llama). Saídas em `eval/results/m10-full/judge/`, `judge_summary.json`, `judge_review.md`
    (tudo ignorado pelo git).
  - **Veredito do critério (fixado em `judge_stats.py` ANTES de ver os números; kappa >= 0,6 e acurácia >= 0,80 no conjunto
    verificável, n=80): NENHUM dos dois juízes serve.** Qwen-juiz: acurácia 59% (IC 48-69%), kappa 0,17 (IC -0,01 a 0,37).
    Llama-juiz: acurácia 78% (IC 67-85%), kappa 0,23 (IC 0,00 a 0,46). Para referência, aprovar tudo daria 74% de acurácia
    (o scorer aprova 73,75%) com kappa 0. As notas dos dois entram no relatório marcadas "NÃO CONFIÁVEL"; a summarization
    segue só pelo scorer determinístico e pela revisão humana.
  - **Por categoria (n=16 cada, IC largo):** `instruction` é a única em que os dois concordam bem com o scorer (acurácia 94%,
    kappa 0,76). Qwen-juiz: classification 50%, extraction 62%, reasoning 69%, tool_use 19%. Llama-juiz: classification 94%
    (kappa 0,64), extraction 69%, reasoning 44% (kappa 0), tool_use 88% (kappa 0,43).
  - **Por que o juiz erra (dos dados, sem generalizar):** (1) ele não consegue resolver a tarefa que julga, então não
    verifica: o Llama-juiz aprovou 17 das 21 respostas que o scorer reprovou, com justificativa inventada (aprovou "27" para uma
    idade cuja resposta é 26; mediana de 4 caracteres nas aprovações indevidas, ou seja, NÃO foi o caso de resposta longa e
    bem escrita, foram respostas curtas e erradas); 93,75% de aprovação no conjunto verificável, quase só notas 4 e 5. (2) O Qwen-juiz é
    severo e incoerente: 33 notas 1 em 96, reprovou 26 das 59 respostas que o scorer aprovou (13 das 14 de `tool_use`), com
    motivo que contradiz a nota ("resposta correta", nota 3) e erros alucinados ("deveria ser fahrenheit"). (3) Nenhum
    discrimina summarization: o Llama-juiz deu 4 às 16, o Qwen-juiz deu 3 a 15 das 16.
  - **Pareado:** inutilizável como medida de qualidade. Viés de posição 67% (32 de 48) no Qwen-juiz e 79% (38 de 48) no
    Llama-juiz (mudaram de lado só por causa da ordem); preferência final 5 Qwen x 5 Llama x 38 empates no primeiro e
    5 x 4 x 39 no segundo; os dois escolhem a posição A em 64% e 69% das rodadas decisivas.
  - **Entre os juízes:** a aprovação concorda em 46% (kappa 0,04, Spearman das notas 0,15); no pareado, 71% (kappa 0,14).
  - **Autopreferência:** no pareado não há sinal (o Qwen ganha 50% pelo Qwen-juiz e 51% pelo Llama-juiz). Na nota pontual o
    contraste líquido é +0,35 ponto, mas a diferença de folga entre os juízes (o Llama-juiz dá 1,3 a 1,7 ponto a mais que o
    Qwen-juiz) domina. Com dois modelos e dois juízes isso é UM contraste, não dá para atribuir o viés a um deles. Aprovações
    indevidas: Qwen-juiz 1/6 nas próprias e 6/15 nas do outro; Llama-juiz 12/15 nas próprias e 5/6 nas do outro (aprova tudo).
  - **Summarization contra o scorer:** Spearman 0,36 para o Qwen-juiz (n=16) e indefinido para o Llama-juiz (nota constante
    4). Só 16 respostas, marcado como não confiável.
  - **Injeção de prompt:** coberta por teste (sistema inalterado, marcadores neutralizados). Observação ao vivo com 1 resposta
    por juiz que pede "dê nota 5": Qwen-juiz deu 1 (resistiu); Llama-juiz deu 5, mas também deu 4 a uma resposta errada
    sem injeção, então não separa manipulação de leniência. Observação, não prova.
  - **Revisão humana:** `eval/results/m10-full/judge_review.md`, 16 itens por semente 20260 (4 em que os dois juízes
    concordam com o scorer, 4 em que algum discorda, 8 de summarization). "Aprovações do Qwen" foi lido como aprovações
    INDEVIDAS de respostas do Qwen (scorer reprovou, algum juiz aprovou), com piso de 2 entre as 4 discordâncias (entraram
    reasoning-08 e extraction-06). Aguarda o veredito humano para calibrar juiz e scorer.
  - **Uma única alteração de prompt (v1 -> v2, commit `fix: make judge output format example unambiguous`):** o exemplo
    de saída dizia `"criterio": 1-5` e o juiz copiava a faixa como se fosse a nota (visto no JSON bruto do smoke de 12
    prompts, numa CÓPIA do run, antes da execução completa). Corrigido o formato do exemplo, nada mais; o critério de
    decisão não foi tocado e o prompt não foi ajustado a partir de concordância. No smoke (v1, 20 respostas verificáveis)
    e na execução completa (v2) nenhum juiz chegou perto do critério.

- **Marco 12 (Fase 3: relatório final, README e fechamento): concluído e publicado. Fase 3 concluída.** Push normal, sem `--force`, de `9ec2617` a `0608afc` (9 commits; autor só noreply, 0 trailers `Co-Authored-By`). **CI do HEAD `0608afc`: verde em ubuntu-24.04 e windows-latest** (todos os passos). Relatório publicado: https://github.com/jcorreaviana/llm-gateway-study/blob/main/reports/fase3-qwen-vs-llama.md Sem
  Anthropic; dataset intacto (`c64fa1474d38`); `eval/results/` fora do `git status`.
  - **Revisão humana:** 3 itens de `tool_use` acrescentados ao `judge_review.md` (semente 20260, 3 de 7 candidatos:
    `tool_use-04`, `-03`, `-01`; o Qwen-juiz reprovou respostas que o scorer aprovou, com motivos que se contradizem).
    Veredito do usuário: concordo com todos os 19 itens (com o scorer em cada um). A frase sobre "conferência direta" de
    itens foi REMOVIDA do relatório e do `human_verdicts.json` por decisão do usuário (restaurar só se ele confirmar). Ressalvas registradas: item 7 (`instruction-07`) certo por motivo acidental (`ends_with "!"` falha por causa da
    aspa final; não existe checagem de "sem aspas"); itens 9 e 15 (summarization) dão crédito a pontos-chave compostos, então
    as aprovações da summarization são um teto. Gravado em `eval/results/m10-full/human_verdicts.json` (local). Concordância
    humano x scorer: 16/16 (8/8 verificáveis, 8/8 summarization), n pequeno; os 3 extras ficam fora da tabela (selecionados
    por característica). A tabela juiz x humano foi removida do relatório (7.2 virou uma frase): o humano decidiu vendo o scorer, então coincide com ele.
  - **Gerador:** `python -m evalkit.report --run-id <id>` grava `reports/fase3-qwen-vs-llama.md` e `.csv` (versionados;
    `git check-ignore` confirma que `reports/` NÃO é ignorado). Determinístico (data do `meta.json`; GPU do `nvidia-smi`, ou
    `--gpu`). 9 seções, 6 avisos obrigatórios (constantes `W_*` testadas). Só agregados e ids: as linhas de `scores.jsonl` passam
    por `sanitize_score_row` (`details` vira só `format_clean`), do juiz só se leem números e rótulos (`judge_view`), e
    `find_leaks` recusa gravar se algum trecho de resposta ou motivo de juiz (>= 12 caracteres) aparecer na saída.
  - **Testes:** 33 novos (`tests/test_report.py`) mais 3 de ordenação; suíte 692 passando + 10 de integração; cobertura 96,56%; ruff limpo;
    `check_secrets.py` limpo (arquivos e `--history`); sem caminho absoluto em `reports/`. 3 quebras intencionais: aviso de
    escopo removido (1 teste falha), filtro `sanitize_score_row` aberto (2 falham; o teste ponta a ponta de canário NÃO
    detecta, porque o relatório não renderiza `details`), trava `find_leaks` desligada (2 falham).
  - **README:** seção "Fase 3: avaliação" (Mermaid validado com o Mermaid CLI, comandos, resultado em 5 linhas, limitações) e
    Roadmap atualizado (Fase 3 concluída, 3b e 2 planejadas). Clone limpo: instalação, `validate_dataset`, 685 testes e
    geração do relatório com a fixture (CLI) funcionaram sem correção; só o `m10-full` não existe no clone (esperado, é local).
  - **Ordenação determinística (autorizada) e CI do Marco 11:** o CI vermelho no ubuntu era falta de `sorted` em
    `tests/test_judge.py::test_execute_runs_every_judgment_and_records_meta` (defeito do teste); corrigido. Varredura de
    `glob`/`rglob`/`iterdir`/`listdir` em `tests/`, `evalkit/`, `scripts/` e `gateway/`: o resto já estava ordenado. O teste novo
    (`tests/test_result_ordering.py`, 3 testes, embaralha linhas e ordem de criação dos arquivos) achou um defeito de produção
    que a varredura não via: `evalkit.score.score_run` gravava o `scores.jsonl` na ordem de chegada das linhas; agora ordena por
    (provedor, `prompt_id`). `summary.json` e o relatório já eram independentes da ordem.
  - **Redação da revisão humana:** a Seção 7 e a Seção 8 do relatório e o README dizem que há um único revisor, 16 itens, e que o
    revisor viu a pontuação do scorer (possível ancoragem): 16/16 é verificação de sanidade, não validação independente.
    Também suavizadas frases causais sem teste (`reasoning` "pode penalizar", efeito não medido; autopreferência).
  - **Ajustes finais de divulgação (commit `docs: tighten phase 3 report disclosures`, pedidos na revisão do usuário):** removida a
    frase de "conferência direta" dos itens (também do `human_verdicts.json`); Seção 8 diz que a coleta foi com alterações não
    commitadas (reprodução exata pelo commit impossível; hashes do dataset e do config verificam essas duas partes); nova Seção 3.1
    divulga a correção do scorer de summarization (bug de contagem da frase de abertura, scores pós-correção calculados em
    2026-10-14T19:40:32Z; o git tem um único commit do scorer, então não prova a ordem dos eventos nem quantos scores mudaram);
    Seção 7.2 virou uma frase (tabela juiz x humano removida); Seção 6.3 define viés de posição e inconsistência de ordem com
    numerador e denominador (Llama-juiz: 38/48 nos dois, 39 empates = 38 por inconsistência + 1 em que as duas rodadas empataram);
    abertura da Seção 7 diz "sorteio estratificado" (4 verificáveis por concordância, 4 por discordância). Geração duas vezes:
    saída idêntica; nenhuma resposta bruta no relatório.
  - **Regra do CI:** o CI só conta como verde depois do push de verificação (um run verde nas duas plataformas, ubuntu-24.04 e
    windows-latest, no HEAD empurrado); testes locais no Windows não provam o Linux. Se o CI ficar vermelho: parar e mostrar o
    job, o passo e o trecho do log.

## Decisões
- **Sem coautoria, daqui em diante:** nenhum commit leva trailer `Co-Authored-By` nem menção a coautoria.
  `--force` volta a ser proibido (a autorização de 14/10/2026 foi de uso único e já foi consumida).
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

- **Fase 3 sem Anthropic (regra desta fase):** `ENABLE_REAL_API=0`, `ANTHROPIC_API_KEY` vazia nos filhos, o
  provedor `anthropic` nunca entra na chain de avaliação (o runner recusa se entrar ou se estiver habilitado) e o
  código de `evalkit/` não importa nem referencia cliente da Anthropic (testado por AST, texto e `sys.modules`).
  Se um baseline do Claude virar útil, é uma fase separada, com autorização. Formato previsto, NÃO implementado:
  `eval/baseline/<prompt_id>.json` (ignorado pelo git).
- **O que a Fase 3 mede:** Qwen (qwen2.5:3b) contra Llama (llama3.2:3b), via gateway, com provedor forçado. NÃO
  mede a perda em relação a um modelo de mercado; o relatório final deve dizer isso de forma explícita (a frase
  também vai no `meta.json`, campo `scope`).

- Avaliação (Marco 9):
  - Referência de qualidade: `check` por regra (`json_schema`, `exact`, `regex`, `numeric`, `label_in`,
    `constraints`) em todas as categorias, menos `summarization` (`reference_answer` manuscrita + 3 a 4
    `key_points`). O dataset usa 5 dos 6 tipos (nenhum prompt usa `regex`; o tipo é aceito e validado).
    O formato e a semântica pretendida de cada tipo estão no docstring de `evalkit/schema.py`; os scorers
    (Marco 10) devem segui-lo. `json_schema` é um SUBCONJUNTO de JSON Schema (`type`, `properties`, `required`,
    `enum`, `items`, `additionalProperties`) mais `expected` (valores esperados campo a campo): evita dependência
    nova (`jsonschema`) e permite conferir o valor, não só a forma.
  - `tool_use` descreve as funções no texto do prompt e pede UMA chamada `{"name":..., "arguments":{...}}`; o
    gateway não ganhou suporte ao campo `tools`. Argumentos de redação livre (ex.: texto de um lembrete) ficam
    fora do `expected`.
  - Validador: prompt de no máximo 800 tokens estimados e entrada + `max_tokens` dentro do menor contexto da chain
    (3072); padrões do `check_secrets.py` mais e-mail, CPF, CNPJ e telefone. A estimativa (chars/4) está em
    `evalkit/tokens.py`, cópia da do gateway com teste de paridade: `gateway/providers.py` também define o cliente da
    Anthropic e o `evalkit` não pode importá-lo.
  - Runner: config derivado em `eval/results/<run_id>/gateway/<provider>/` com chain só do provedor medido e
    `warmup` desligado no outro Ollama (o gateway aquecia os dois e alternaria modelos na VRAM); porta livre,
    budget e recorder do gateway no diretório do run. Warm-up descartado (um prompt curto) antes da medição.
    Latência medida no cliente (ponta a ponta, inclui o gateway). `status` 0 = sem resposta HTTP (timeout etc.).
    Resultado também guarda `finish_reason` (para detectar truncamento por `max_tokens`) e `provider`.
  - `--limit N` escolhe uma amostra que alterna as categorias (não os N primeiros); `--resume` pula todos os
    `prompt_id` gravados, inclusive os com erro (não há retry automático), descarta uma última linha truncada e
    recusa continuar se o hash do dataset ou do config mudou. Sem `--resume`, não sobrescreve um resultado existente.
  - O runner recusa `ENABLE_REAL_API` ligada no ambiente do usuário e zera `ANTHROPIC_API_KEY` no próprio processo.
    O mock sobe junto (a pilha compartilhada sempre sobe os dois), mas fica fora da chain.
  - `.gitignore`: `*.jsonl` segue ignorado, com exceção explícita `!eval/prompts/*.jsonl`. O
    `eval/prompts.example.jsonl` nunca existiu (só a exceção no `.gitignore`), que foi removida.
  - Fim de linha: o dataset e o config são hasheados com CRLF normalizado para LF (o Git no Windows converte).
  - Dois commits (`feat: add synthetic eval dataset and validator`, `feat: add eval runner`) foram feitos com 1 a 2
    erros de `E501` do ruff e corrigidos com `--amend` antes de qualquer push (local, sem `--force`).

- Scorers e estatística (Marco 10):
  - Contrato: cada scorer devolve `{score, passed, details}` e nunca levanta. Erro do runner (`error`, `status` != 200)
    e resposta vazia viram `errored: true` (score 0), contados em `errors` e FORA da média, da aprovação e da latência.
  - `format_clean` (em `details`) separa obediência de formato de correção: a resposta é limpa (cerca única, prosa
    antes/depois, espaços) antes de pontuar, e o campo diz se a limpeza foi necessária. `passed` só olha o conteúdo.
  - `json_schema`: campos pontuados = folhas de `expected` + campos `required` (recursivo); violação fora desses campos
    (enum de opcional, campo extra com `additionalProperties: false`) entra como campo errado extra. Array no topo = 0.
  - `exact`: padrão do schema (strip, casefold, pontuação final); acentos são opcionais (`ExactOptions`, não exposto no
    spec porque o schema não aceita campos extras). `numeric`: último número; vírgula única é decimal, `1.234` é milhar,
    `3.5` é decimal, `1,234.5` é formato inglês. `label_in`: palavra inteira sem acento/caixa, `_` e `-` valem como espaço.
  - Summarization: 0,6 cobertura + 0,3 similaridade (cosseno com a `reference_answer`, mínimo 0) + 0,1 tamanho; sem
    embedder renormaliza para cobertura + tamanho. Ponto-chave coberto se >= 60% dos seus termos de conteúdo aparecem
    (sem acento/caixa, radical de 5 letras). Aprovado com score >= 0,7. Limite de tamanho lido do enunciado ("até N
    frases", "N tópicos/passos"); resposta cortada por `max_tokens` (`finish_reason: length`) zera só o componente de tamanho.
  - Estatística: IC de Wilson 95%; bootstrap percentil (10 000 reamostragens, semente 20260, `random.Random` local);
    McNemar exato bilateral. O veredito só declara diferença se o IC95% exclui zero E o McNemar dá p < 0,05 E há >= 5
    pares; senão "indicativo, não conclusivo", sem vencedor. Tokens/s = `completion_tokens` / latência ponta a ponta
    (inclui gateway e processamento do prompt).
  - Embedding: carrega só do cache local (`local_files_only=True`); baixar exige `--allow-download`. Dependência fora
    do `requirements.txt` e do CI; os testes padrão usam embedder falso.

- Juiz local (Marco 11):
  - O juiz NÃO é verdade: mede-se primeiro a concordância com os scorers determinísticos (veredito = nota geral >= 4 contra
    `passed`, nas 5 categorias com `check`, n=80) e só então se decide a confiança. Critério fixo em `evalkit/judge_stats.py`
    (kappa >= 0,6 e acurácia >= 0,80, por juiz); kappa indefinido reprova. Não se ajusta o critério nem o prompt pela concordância.
  - `judge_prompts.judge_view` é o ÚNICO filtro entre o dataset e o juiz: só enunciado, `reference_answer` e `key_points`.
    `check`, `notes` e resultado do scorer nunca chegam a ele. A resposta julgada vai entre `<<<RESPOSTA_INICIO/FIM>>>`;
    marcadores iguais dentro dela são neutralizados e o prompt de sistema só depende do tipo de tarefa e do modo.
  - O pareado usa duas rodadas com a ordem trocada; a preferência final só vale se as duas concordam. Índice de viés de posição
    = fração dos prompts em que as duas rodadas escolheram lados diferentes (= mesma posição). Rodada empate x lado conta como
    empate e entra em `order_inconsistency`, não no índice.
  - Saídas em `eval/results/<run>/judge/` (e não no topo): um `*.jsonl` no topo seria lido pelo `evalkit.score` como provedor.
    Os julgamentos passam pelo gateway com o juiz como provedor forçado (mesmas pré-checagens do runner). `--resume` recusa
    continuar se o hash do prompt do juiz ou do dataset mudou.
  - `max_tokens` 256 e `temperature` 0; `judge_error` (JSON inválido depois de UMA nova tentativa, ou erro de transporte) sai da
    calibração e é contado à parte. JSON truncado ainda vale se o `overall` já foi escrito (`recovered`).
  - Autopreferência com dois modelos e dois juízes é um único contraste (no pareado o número é idêntico para os dois por
    construção); o relatório diz isso e mostra também a folga geral e as aprovações indevidas em respostas próprias x do outro.

## Como rodar os testes
`.venv\Scripts\python -m pytest`  (aleatório; `--cov` para cobertura; `-m integration` com Ollama real;
`ruff check .` para lint). Detalhes no README.

## Dívidas conhecidas
- Bucket: `usage.total_tokens` pode deixar o TPM negativo.
- Estado de cooldown, breaker, buckets e métricas só em memória; recorder e budget assumem um processo.
- Estimativa de tokens aproximada (caracteres/4); sem streaming.
- CI: resolvida no Marco 9 (`ubuntu-24.04` fixo, actions v7 em Node 24). Quando for migrar para Ubuntu 26, trocar
  o runner de propósito e rodar o CI.
- Runner de avaliação sobe também o mock (inútil na Fase 3); inofensivo, mas é um processo a mais.
- Validação real única da Anthropic (1 chamada, `max_tokens` 20) nunca feita: só com autorização explícita.

## Próximos passos
1. **Fase 2 (próxima):** comparar o gateway com o LiteLLM Proxy (mesmo cenário de fallback, rate limit e cooldown).
2. **Fase 3b (opcional, depende de autorização explícita):** baseline de um modelo de mercado nos mesmos prompts, para medir a perda
   real. Também: variante de `reasoning` com explicação liberada e mais prompts por categoria para resultados conclusivos.
3. Para repetir a avaliação e regerar o relatório, veja a seção "Fase 3: avaliação" do README (os dados do run `m10-full` são
   locais e não versionados).
