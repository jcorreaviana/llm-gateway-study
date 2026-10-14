# Fase 3: Qwen contra Llama via gateway | run `m10-full`

Dados do run de 2026-10-14 (data lida do `meta.json`). Este relatório traz só agregados e ids de prompts; nenhum texto de resposta de modelo.

## 1. Resumo

1. Escopo: Qwen (qwen2.5:3b) contra Llama (llama3.2:3b) via gateway, em 48 prompts sintéticos em PT-BR (8 por categoria). Não mede a perda em relação a um modelo de mercado; nenhuma chamada à Anthropic.
2. Neste conjunto: aprovação de 87,5% (IC95% 75% a 94%) para o Qwen e 64,6% (IC95% 50% a 77%) para o Llama.
3. Pareado, neste conjunto: diferença média de score 0,15 (IC95% 0,06 a 0,26), McNemar 12 x 1, p=0,0034; diferença detectada a favor do Qwen, sem generalizar para outros modelos ou domínios.
4. Por categoria (8 pares cada), nenhuma comparação é conclusiva: todas são "indicativo, não conclusivo".
5. O juiz local (Qwen e Llama julgando) NÃO é confiável: nenhum dos dois atinge o critério fixado antes da execução; a summarization segue só pelo scorer determinístico e pela revisão humana.

## 2. Escopo e avisos

1. Este relatório mede o Qwen (qwen2.5:3b) contra o Llama (llama3.2:3b), via gateway, com provedor forçado. NÃO mede a perda em relação a um modelo de mercado. Nenhuma chamada à Anthropic foi feita.
2. O dataset é sintético, em PT-BR, com 8 prompts por categoria. Resultados por categoria são "indicativo, não conclusivo" quando o IC inclui zero ou o McNemar não confirma.
3. O enunciado de `reasoning` proíbe explicar a resposta, o que pode penalizar modelos pequenos (sem cadeia de raciocínio escrita); o tamanho desse efeito não foi medido.
4. As latências são de modelo quente em GPU. O tok/s de respostas curtas é dominado por overhead e só é comparável nas categorias com respostas longas.
5. Os scorers de summarization dão crédito parcial a pontos-chave compostos (exemplo: um resumo sem os 2,4 GHz recebeu cobertura 1,0). As aprovações da summarization são um teto, não o valor exato.
6. O scorer de `instruction-07` falha por `ends_with "!"` por causa de aspas na resposta, e não existe checagem explícita de "sem aspas".

## 3. Método

- **Dataset:** sintético, PT-BR, 6 categorias (classification, extraction, instruction, reasoning, summarization, tool_use) x 8 prompts = 48 prompts por provedor. Hash SHA-256 `c64fa1474d38` (completo: `c64fa1474d3849ee2a90bebb610aad980a9ab14a3ac8fd90dbd5ba96aa8908a3`); o hash no momento da pontuação é o mesmo.
- **Modelos:** Qwen = `qwen2.5:3b`, Llama = `llama3.2:3b`. Ollama 0.40.3.
- **Execução:** um provedor por vez, em sequência, via gateway com provedor forçado, `temperature` 0, uma execução por prompt (sem repetição), warm-up descartado antes da medição, `context_window` 3072, `timeout` 120 s. A latência é medida no cliente, ponta a ponta (inclui o gateway). Hash do config de avaliação `d28fc9934c0c`.
- **Scorers (determinísticos):** por regra, `json_schema` (subconjunto de JSON Schema com valores esperados), `exact`, `regex`, `numeric`, `label_in` e `constraints`; erro de execução ou resposta vazia contam como `errors` e ficam fora da média, da aprovação e da latência. Summarization: 0,6 cobertura de pontos-chave + 0,3 similaridade por embedding + 0,1 tamanho; aprovado com score >= 0,7.
- **Embedding:** `sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2`, revision `e8f8c211226b894fcb81acc59f3b34ba3efd5f42`, device `cpu`, `max_seq_length` 128.
- **Estatística:** IC de Wilson 95% para a aprovação; bootstrap percentil (10 000 reamostragens, semente 20260) para a diferença média pareada; McNemar exato bilateral. Só se declara diferença quando o IC95% exclui zero E o McNemar dá p < 0,05 E há pelo menos 5 pares.
- **Commit do código na coleta:** `151b8ec3d4965b6a0721b9f54a96d06de0d51160` (árvore com alterações não commitadas na coleta).
- **GPU:** NVIDIA GeForce RTX 5060.

## 4. Resultados

### 4.1 Por provedor e categoria

| Provedor | Categoria | n | Erros | Score médio | Aprovação | IC95% (Wilson) |
|---|---|---|---|---|---|---|
| Qwen | geral | 48 | 0 | 0,89 | 87,5% | 75% a 94% |
| Qwen | classification | 8 | 0 | 1,00 | 100,0% | 68% a 100% |
| Qwen | extraction | 8 | 0 | 0,97 | 87,5% | 53% a 98% |
| Qwen | instruction | 8 | 0 | 0,97 | 87,5% | 53% a 98% |
| Qwen | reasoning | 8 | 0 | 0,50 | 50,0% | 22% a 78% |
| Qwen | summarization | 8 | 0 | 0,91 | 100,0% | 68% a 100% |
| Qwen | tool_use | 8 | 0 | 1,00 | 100,0% | 68% a 100% |
| Llama | geral | 48 | 0 | 0,74 | 64,6% | 50% a 77% |
| Llama | classification | 8 | 0 | 0,75 | 75,0% | 41% a 93% |
| Llama | extraction | 8 | 0 | 0,71 | 50,0% | 22% a 78% |
| Llama | instruction | 8 | 0 | 0,90 | 75,0% | 41% a 93% |
| Llama | reasoning | 8 | 0 | 0,38 | 37,5% | 14% a 69% |
| Llama | summarization | 8 | 0 | 0,80 | 75,0% | 41% a 93% |
| Llama | tool_use | 8 | 0 | 0,91 | 75,0% | 41% a 93% |

Aprovação = fração de respostas com `passed` (sobre respostas avaliáveis). Com 8 prompts por categoria os ICs são largos. As aprovações da summarization são um teto (aviso 5). Em `reasoning` o enunciado proíbe explicar (aviso 3).

### 4.2 Comparação pareada (Qwen menos Llama)

| Categoria | Pares | Dif. média de score | IC95% da diferença | McNemar | p exato | Leitura |
|---|---|---|---|---|---|---|
| geral | 48 | 0,15 | 0,06 a 0,26 | 12 x 1 | 0,0034 | diferença detectada |
| classification | 8 | 0,25 | 0,00 a 0,62 | 2 x 0 | 0,5000 | indicativo, não conclusivo (IC inclui zero) |
| extraction | 8 | 0,26 | 0,06 a 0,51 | 3 x 0 | 0,2500 | indicativo, não conclusivo (IC exclui zero, McNemar não confirma) |
| instruction | 8 | 0,07 | 0,00 a 0,17 | 1 x 0 | 1,0000 | indicativo, não conclusivo (IC inclui zero) |
| reasoning | 8 | 0,12 | -0,25 a 0,50 | 2 x 1 | 1,0000 | indicativo, não conclusivo (IC inclui zero) |
| summarization | 8 | 0,11 | 0,04 a 0,19 | 2 x 0 | 0,5000 | indicativo, não conclusivo (IC exclui zero, McNemar não confirma) |
| tool_use | 8 | 0,09 | 0,00 a 0,22 | 2 x 0 | 0,5000 | indicativo, não conclusivo (IC inclui zero) |

Dif. média = média por prompt de (score do Qwen menos score do Llama). McNemar = prompts em que só o Qwen aprova x prompts em que só o Llama aprova. O resultado geral pode ser afirmado "neste conjunto"; as linhas por categoria só valem como "indicativo, não conclusivo" quando a leitura diz isso, e nesse caso não nomeiam vencedor. Nenhuma causa foi testada.

### 4.3 Prompts em que os modelos reprovam (ids)

- Os dois reprovam: `extraction-06`, `instruction-08`, `reasoning-03`, `reasoning-05`, `reasoning-08`.
- Só o Qwen reprova (1): `reasoning-07`.
- Só o Llama reprova (12): `classification-05`, `classification-06`, `extraction-02`, `extraction-07`, `extraction-08`, `instruction-07`, `reasoning-01`, `reasoning-02`, `summarization-03`, `summarization-04`, `tool_use-05`, `tool_use-08`.

Estas listas descrevem prompts, não um ranking por categoria.

A reprovação em `instruction-07` está correta, mas por motivo acidental do scorer (aviso 6): O scorer de `instruction-07` falha por `ends_with "!"` por causa de aspas na resposta, e não existe checagem explícita de "sem aspas".

### 4.4 Obediência de formato (`format_clean`)

| Provedor | Respostas avaliáveis | Precisaram de limpeza | ids |
|---|---|---|---|
| Qwen | 48 | 0 | nenhum |
| Llama | 48 | 2 | `reasoning-06`, `reasoning-07` |

`format_clean` falso = a resposta veio com cerca de código ou prosa ao redor e foi limpa antes de pontuar. A correção do conteúdo (`passed`) é medida depois da limpeza, então este número mostra a obediência de formato separada da correção.

## 5. Latência e tok/s

As latências são de modelo quente em GPU. O tok/s de respostas curtas é dominado por overhead e só é comparável nas categorias com respostas longas.

| Provedor | Categoria | p50 (ms) | p95 (ms) | tok/s p50 | tok/s comparável |
|---|---|---|---|---|---|
| Qwen | geral | 212 | 702 | 119,3 | - |
| Qwen | classification | 63 | 85 | 50,0 | não (resposta curta) |
| Qwen | extraction | 294 | 514 | 128,9 | sim |
| Qwen | instruction | 153 | 301 | 117,3 | sim |
| Qwen | reasoning | 60 | 72 | 50,7 | não (resposta curta) |
| Qwen | summarization | 686 | 784 | 143,0 | sim |
| Qwen | tool_use | 259 | 342 | 119,8 | sim |
| Llama | geral | 230 | 718 | 123,2 | - |
| Llama | classification | 63 | 76 | 51,1 | não (resposta curta) |
| Llama | extraction | 257 | 390 | 126,5 | sim |
| Llama | instruction | 260 | 498 | 132,2 | sim |
| Llama | reasoning | 50 | 63 | 42,8 | não (resposta curta) |
| Llama | summarization | 646 | 994 | 142,6 | sim |
| Llama | tool_use | 250 | 331 | 123,3 | sim |

Latência ponta a ponta no cliente (inclui o gateway). tok/s = `completion_tokens` / tempo da requisição (inclui o processamento do prompt). Categorias de resposta curta (mediana abaixo de 10 tokens): `classification`, `reasoning`. Uma execução por prompt, sem repetição; as diferenças de poucos milissegundos entre provedores não devem ser interpretadas.

## 6. Juiz local

Um modelo de 3B julgando respostas de modelos de 3B. O juiz NÃO é tratado como verdade: mede-se primeiro a concordância dele com os scorers determinísticos e só então se decide quanto confiar.

**Critério, fixado antes da execução** (em `evalkit/judge_stats.py`): kappa >= 0,6 e acurácia >= 80%, por juiz, no conjunto verificável (nota geral >= 4 = aprova; todas as categorias com check (extraction, classification, reasoning, instruction, tool_use)). Kappa indefinido reprova.

### 6.1 Calibração contra o scorer (conjunto verificável)

| Juiz | n | Acurácia | IC95% | Kappa | IC95% do kappa | Aprovação do juiz | VP/FP/FN/VN |
|---|---|---|---|---|---|---|---|
| Qwen-juiz | 80 | 58,8% | 48% a 69% | 0,17 | -0,01 a 0,37 | 50,0% | 33/7/26/14 |
| Llama-juiz | 80 | 77,5% | 67% a 85% | 0,23 | 0,00 a 0,46 | 93,8% | 58/17/1/4 |

**Linha de base "aprovar tudo":** acurácia 73,8% (é a taxa de aprovação do scorer) e kappa 0. Comparação: Qwen-juiz 58,8% (abaixo da linha, 15,0 p.p.); Llama-juiz 77,5% (acima da linha, 3,7 p.p.).

**Veredito:** Qwen-juiz: NÃO CONFIÁVEL: não usar as notas do juiz nas tarefas abertas; Llama-juiz: NÃO CONFIÁVEL: não usar as notas do juiz nas tarefas abertas.

As notas dos dois juízes ficam marcadas "NÃO CONFIÁVEL" e não entram nos resultados da Seção 4. A summarization segue só pelo scorer determinístico e pela revisão humana.

### 6.2 Por categoria (n=16 cada, IC largo)

| Categoria | Qwen-juiz acurácia | Qwen-juiz kappa | Llama-juiz acurácia | Llama-juiz kappa |
|---|---|---|---|---|
| classification | 50% | 0,16 | 94% | 0,64 |
| extraction | 62% | 0,02 | 69% | 0,00 |
| instruction | 94% | 0,76 | 94% | 0,76 |
| reasoning | 69% | 0,35 | 44% | 0,00 |
| tool_use | 19% | 0,02 | 88% | 0,43 |

`instruction` é a única categoria em que os dois juízes concordam bem com o scorer (acurácia 94%, kappa 0,76, IC95% do kappa 0,00 a 1,00, n=16). É uma exceção de amostra pequena, não uma licença para usar o juiz nas outras categorias; nas demais, veja a tabela acima.

### 6.3 Comparação pareada pelo juiz

| Juiz | Viés de posição | Inconsistência de ordem | Prefere Qwen | Prefere Llama | Empates |
|---|---|---|---|---|---|
| Qwen-juiz | 67% | 79% | 5 | 5 | 38 |
| Llama-juiz | 79% | 79% | 5 | 4 | 39 |

Viés de posição = fração de prompts em que o juiz escolheu a mesma POSIÇÃO nas duas rodadas (com a ordem trocada). Com índices desse tamanho e quase todos os resultados em empate, o pareado do juiz é inutilizável como medida de qualidade.

### 6.4 Entre juízes, autopreferência e injeção

- **Entre juízes:** concordância de aprovação 46% (kappa 0,04, Spearman das notas 0,15); no pareado, 71% (kappa 0,14). As notas não são promediadas.
- **Autopreferência: sem evidência suficiente.** Com dois modelos e dois juízes, o contraste observado é UM só (no pareado o número é idêntico para os dois juízes por construção) e não permite dizer qual juiz favorece o próprio modelo; parte do efeito pode ser real, porque o Qwen acerta mais no conjunto verificável. Aprovações indevidas (scorer reprovou, juiz aprovou): Qwen-juiz 1/6 nas próprias e 6/15 nas do outro; Llama-juiz 12/15 nas próprias e 5/6 nas do outro. Não há evidência suficiente nem para afirmar nem para descartar viés.
- **Injeção de prompt: sem evidência suficiente.** A defesa (marcadores neutralizados, prompt de sistema que não depende da resposta) é coberta por teste; a observação ao vivo teve 1 resposta por juiz e não separa manipulação de leniência. Não se afirma resistência.
- **Summarization:** correlação de Spearman das notas do juiz com o scorer: Qwen-juiz 0,36, Llama-juiz n/d (nota constante, indefinida) (n=16 respostas; intervalo muito largo).

### 6.5 Divulgação: o prompt do juiz mudou uma vez

O prompt do juiz teve uma única alteração, da versão v1 para a v2, por um bug de formato: o exemplo de saída trazia a faixa (`1-5`) no lugar de uma nota e o juiz a copiava. A correção mexeu só no exemplo de formato; o critério de decisão não foi tocado e o prompt NÃO foi ajustado a partir de concordância. O problema foi visto no JSON bruto de um teste-fumaça de 12 prompts (numa cópia do run), antes da execução completa; nesse teste e na execução completa nenhum juiz chegou perto do critério. Versão usada nos números acima: `v2`.

## 7. Revisão humana

O revisor humano olhou 16 itens sorteados (semente 20260): 8 de tarefas verificáveis e 8 de summarization, e mais 3 itens de `tool_use` escolhidos depois (respostas do Qwen que o scorer aprovou e o Qwen-juiz reprovou). Para cada item viu o enunciado, a resposta, o resultado do scorer e as notas dos juízes. Registro: concordo com todos os 19 itens, ou seja, com a pontuação do scorer determinístico em cada um.

**Como ler esta seção:** há um único revisor, a amostra é de 16 itens e o revisor VIU a pontuação do scorer ao decidir (possível ancoragem). Por isso a concordância abaixo é uma verificação de sanidade, e não uma validação independente do scorer.

### 7.1 Concordância humano x scorer (verificação de sanidade)

| Conjunto | n | Humano concorda com o scorer |
|---|---|---|
| Todos os itens do sorteio | 16 | 16/16 |
| Verificáveis | 8 | 8/8 |
| Summarization | 8 | 8/8 |

Os 3 itens extras de `tool_use` (3/3 de concordância) foram escolhidos por uma característica (o Qwen-juiz reprovou) e ficam FORA da tabela: seriam uma amostra enviesada.

**Ressalva de amostra pequena:** n=16 no total (8 verificáveis, 8 de summarization). Uma concordância de 16/16 com n assim não valida o scorer: o intervalo de confiança é largo, o revisor decidiu vendo o resultado do scorer, e o revisor registrou conferência direta de 6 dos 19 itens (itens 7, 9, 15, 17, 18, 19). Os itens sorteados para ter discordância entre juiz e scorer estão sobrerrepresentados.

### 7.2 Juiz x humano (o veredito humano é o do scorer, não é independente)

| Juiz | Verificáveis | Summarization | Todos (n=16) | Extras de tool_use |
|---|---|---|---|---|
| Llama-juiz | 4/8 | 7/8 | 11/16 | 3/3 |
| Qwen-juiz | 8/8 | 1/8 | 9/16 | 0/3 |

Valores = itens em que a aprovação do juiz (nota geral >= 4) coincide com o veredito humano. É a mesma medida da Seção 6, agora contra o humano, em amostra pequena e enviesada para discordâncias.

### 7.3 Ressalvas do revisor (sem mudança no scorer nem no dataset)

- Item(ns) 7 (`instruction-07`): resultado certo por motivo acidental: ends_with "!" falha por causa da aspa final; não existe checagem explícita de "sem aspas".
- Item(ns) 9 (`summarization-02`), 15 (`summarization-07`): o scorer de summarization dá crédito a pontos-chave compostos (o resumo sem os 2,4 GHz recebe cobertura 1,0); as aprovações da summarization são um teto.

## 8. Limitações

- Compara só dois modelos de 3B, em um conjunto sintético de 48 prompts em PT-BR (8 por categoria); nada aqui se generaliza para outros modelos, tamanhos ou domínios.
- Não há baseline de um modelo de mercado: não se sabe quanto qualidade se perde em relação a ele.
- Por categoria, os ICs são largos e nenhuma comparação é conclusiva; só o resultado geral pareado é afirmado, e apenas "neste conjunto".
- Uma execução por prompt, `temperature` 0, sem repetição: não mede a variação entre execuções.
- A referência de qualidade são regras escritas à mão (e, na summarization, pontos-chave); regras podem errar para os dois lados (avisos 5 e 6). `reasoning` penaliza respostas sem explicação (aviso 3).
- Latências de modelo quente em GPU, uma máquina, sem concorrência (aviso 4).
- O juiz local não é confiável; a revisão humana cobre 16 itens (mais 3 extras), foi feita por uma só pessoa que viu a pontuação do scorer, e é uma verificação de sanidade, não uma validação independente.
- Sem causalidade testada: diferenças observadas não são atribuídas a nenhuma causa.

## 9. Próximos passos

1. **Fase 3b, baseline do Claude:** rodar os mesmos prompts em um modelo de mercado para medir a perda real. Depende de autorização explícita (envolve chamadas pagas) e de uma fase própria.
2. **Variante de `reasoning` com explicação liberada**, para separar a capacidade de raciocinar do efeito da proibição de explicar (aviso 3).
3. **Mais prompts por categoria**, para que as comparações por categoria possam ser conclusivas.
4. **Fase 2, LiteLLM:** comparar o gateway com o LiteLLM Proxy no mesmo cenário de fallback, rate limit e cooldown.
