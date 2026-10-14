# Matriz de testes: requisito -> teste

Mapa dos requisitos do projeto para os testes que os protegem. Os testes ficam em `tests/`.
Nenhum teste fala com a internet nem com a Anthropic (só `httpx.MockTransport`, ASGI em memória
e, nos testes de integração opcionais, processos locais em `127.0.0.1`).

Legenda da coluna **Situação**: `ok` = já coberto; `novo` = lacuna encontrada no Marco 6 e
preenchida pelo teste indicado.

## Gatilhos de fallback

| Requisito | Teste(s) | Situação |
|---|---|---|
| 429 do provedor -> fallback (`rate_limited`) | `test_gateway_routing.py::test_mock_failure_falls_back_to_qwen[chaos0-*]`, `test_anthropic.py::test_http_failures_are_classified_and_still_count_in_budget[429-*]` | ok |
| 5xx -> fallback (`server_error`) | `test_gateway_routing.py::test_mock_failure_falls_back_to_qwen[chaos1/2-*]`, `test_anthropic.py::...[500/502/529-*]` | ok |
| Timeout -> fallback (`timeout`) e cancelamento real | `test_gateway_routing.py::test_network_failures_fall_back[exc0/exc1]`, `test_resilience.py::test_timeout_cancels_request_for_real_and_counts_for_breaker` | ok |
| Erro de conexão -> fallback | `test_gateway_routing.py::test_network_failures_fall_back[exc2-*]` | ok |
| 401/403 -> fallback (`auth_error`) | `test_gateway_routing.py::test_auth_error_falls_back`, `test_anthropic.py::...[401/403-*]` | ok |
| 2xx sem JSON -> fallback (`invalid_response`) | `test_anthropic.py::test_invalid_2xx_is_invalid_response` | ok |
| Token bucket local esgotado -> fallback (`rate_limit_local`) | `test_resilience.py::test_local_rate_limit_skips_provider_then_recovers`, `test_local_tpm_uses_real_usage`, `test_full_chain_fallback_for_each_new_reason[rate_limit_local]` | ok |
| Circuit breaker aberto -> fallback (`breaker_open`) | `test_resilience.py::test_breaker_opens_after_consecutive_failures_and_stops_calling_provider`, `test_full_chain_fallback_for_each_new_reason[breaker_open]` | ok |
| Cooldown por `Retry-After` -> fallback (`cooling_down`) | `test_resilience.py::test_429_triggers_cooldown_without_counting_for_breaker`, `test_429_without_retry_after_uses_default`, `test_full_chain_fallback_for_each_new_reason[cooling_down]` | ok |
| Prompt maior que a janela -> pulado (`context_too_large`) | `test_gateway_routing.py::test_context_too_large_skips_provider` | ok |
| Provedor desabilitado -> pulado (`disabled`) | `test_gateway_routing.py::test_disabled_provider_is_skipped_and_reported` | ok |
| 4xx do cliente -> sem fallback, corpo repassado | `test_gateway_routing.py::test_client_error_is_returned_without_fallback` | ok |
| Cadeia inteira falha -> 503 com motivos por provedor | `test_gateway_routing.py::test_all_failing_returns_503_with_reasons`, `test_resilience.py::test_all_unavailable_returns_503_with_reasons_and_retry_after`, `test_503_without_waiting_info_has_no_retry_after` | ok |
| Fallback real contra o mock em `rate_limit` e Ollama de verdade | `test_integration.py::test_real_fallback_mock_rate_limit_to_ollama` (opcional) | novo |

## Circuit breaker

| Requisito | Teste(s) | Situação |
|---|---|---|
| Abre após N falhas consecutivas; sucesso zera | `test_breaker.py::test_opens_after_consecutive_failures`, `test_success_resets_failure_counter` | ok |
| `open` -> `half_open` após o cooldown; sonda fecha ou reabre | `test_breaker.py::test_open_becomes_half_open_after_cooldown`, `test_successful_probe_closes`, `test_failed_probe_reopens_and_restarts_cooldown` | ok |
| Limite de sondas concorrentes; resultado velho ignorado | `test_breaker.py::test_half_open_limits_concurrent_probes`, `test_stale_result_from_before_transition_is_ignored`; `test_resilience.py::test_cancelled_request_releases_probe_slot` | ok |
| 429 nunca abre o breaker | `test_resilience.py::test_many_429s_never_open_the_breaker` | ok |

## Token bucket

| Requisito | Teste(s) | Situação |
|---|---|---|
| Reabastecimento contínuo, RPM e TPM independentes | `test_ratelimit.py::test_bucket_refills_continuously_not_by_window`, `test_rpm_and_tpm_are_independent` | ok |
| Bloqueio e tempo de espera; `None` = sem limite | `test_ratelimit.py::test_bucket_blocks_when_exhausted_and_reports_wait`, `test_none_means_unlimited` | ok |
| Pedido maior que o balde; correção por `usage` real | `test_ratelimit.py::test_oversized_request_is_capped_at_capacity`, `test_usage_adjusts_consumption_to_real_value`, `test_invalid_usage_is_ignored` | ok |

## Budget e chamada real

| Requisito | Teste(s) | Situação |
|---|---|---|
| Limite rígido, sem reset automático | `test_budget.py::test_blocks_at_limit_and_never_resets`, `test_default_limit_is_30` | ok |
| Reserva ANTES de enviar; falha também gasta | `test_anthropic.py::test_http_failures_are_classified_and_still_count_in_budget`, `test_error_loop_cannot_exceed_budget` | ok |
| Arquivo corrompido = esgotado, nunca reiniciado | `test_budget.py::test_corrupt_or_invalid_file_counts_as_exhausted_and_is_not_reset`, `test_unreadable_file_counts_as_exhausted` | ok |
| Concorrência (threads, tarefas, processos) não passa do limite | `test_budget.py::test_threads_cannot_exceed_limit`, `test_concurrent_async_tasks_cannot_exceed_limit`, `test_processes_cannot_exceed_limit` | ok |
| Escrita atômica | `test_budget.py::test_write_is_atomic_via_os_replace`, `test_failed_replace_keeps_old_file_and_leaves_no_temp` | ok |
| Reset manual pede confirmação | `test_budget.py::test_reset_requires_confirmation`, `test_reset_yes_flag_skips_prompt` | ok |
| Budget esgotado cai para o próximo sem tocar o breaker | `test_anthropic.py::test_gateway_budget_exhausted_falls_back_without_touching_breaker`, `test_gateway_routing.py::test_anthropic_exhausted_budget_blocks_even_with_flag_and_key` | ok |
| Contador de chamadas reais e budget restante nas métricas | `test_metrics_recorder.py::test_real_api_calls_and_budget_remaining`, `test_budget_exhausted_does_not_count_as_real_call` | ok |

## `AnthropicProvider` bloqueado com `ENABLE_REAL_API=0`

| Requisito | Teste(s) | Situação |
|---|---|---|
| Flag desligada ou ausente: nenhuma conexão | `test_anthropic.py::test_blocked_when_flag_is_off_or_absent`, `test_gateway_real_api_blocked_falls_back_without_touching_breaker` | ok |
| Forçar o `anthropic` com a flag desligada: budget intacto | `test_anthropic.py::test_gateway_forced_anthropic_blocked_leaves_budget_untouched` | ok |
| Sem chave / provedor desabilitado / entrada grande | `test_anthropic.py::test_blocked_without_key`, `test_blocked_when_provider_disabled`, `test_input_too_large_blocks_without_spending_budget` | ok |
| Host fixo, não redirecionável pelo request | `test_anthropic.py::test_base_url_cannot_be_redirected_by_request` | ok |
| `max_tokens` com teto e `model` do config | `test_anthropic.py::test_max_tokens_is_capped`, `test_translates_request_and_response` | ok |
| Caminho real ponta a ponta gasta exatamente uma reserva | `test_anthropic.py::test_gateway_real_path_end_to_end_spends_one_reservation` | ok |

## `x-gateway-force-provider`

| Requisito | Teste(s) | Situação |
|---|---|---|
| Ignorado quando `force_provider_enabled: false` | `test_gateway_routing.py::test_force_header_ignored_when_disabled` | ok |
| Ligado: tenta só aquele provedor, sem fallback | `test_gateway_routing.py::test_force_header_works_when_enabled_without_fallback` | ok |
| Nome desconhecido -> 400 | `test_gateway_routing.py::test_force_unknown_provider_returns_400` | ok |
| Respeita cooldown, breaker e bucket | `test_resilience.py::test_force_provider_respects_cooldown_bucket_and_breaker` | ok |

## Segredos

| Requisito | Teste(s) | Situação |
|---|---|---|
| Chave ausente de logs, falhas e corpo de erro | `test_anthropic.py::test_key_never_appears_in_logs_failures_or_client_error_body` | ok |
| Chave ausente de `/status`, `/metrics`, recorder e resposta | `test_anthropic.py::test_key_never_leaks_through_status_metrics_recorder_or_response` | ok |
| Logs de aplicação sem prompt, resposta ou chave | `test_gateway_routing.py::test_logs_do_not_contain_prompt`, `test_metrics_recorder.py::test_app_logs_still_have_no_prompt_response_or_key` | ok |
| Recorder mascara segredos no conteúdo | `test_metrics_recorder.py::test_secrets_are_masked_in_recorded_content` | ok |
| Canário de segredo em TODOS os cenários (sucesso, 429, 500, 401, timeout, 503, budget esgotado) e em todas as saídas | `test_secret_canary.py` | novo |
| Segredo no PROMPT do usuário é mascarado no recorder | `test_secret_canary.py::test_secret_in_user_prompt_is_masked_by_recorder` | novo |
| `/status` e `/metrics` não expõem prompt nem resposta | `test_secret_canary.py::test_status_and_metrics_expose_no_prompt_or_response` | novo |
| `check_secrets.py` detecta padrões e não imprime o valor | `test_check_secrets.py` | ok |
| `logs/` e `data/` ignorados pelo git | `test_metrics_recorder.py::test_runtime_files_are_git_ignored` | ok |

## Isolamento e confiabilidade da suíte

| Requisito | Teste(s) | Situação |
|---|---|---|
| Nenhum teste escreve em `data/` ou `logs/` do repositório | `conftest.py` (`_isolate_project_paths`, autouse) + `test_suite_isolation.py` | novo |
| Nenhum teste abre conexão para fora de `127.0.0.1`/`localhost` | `conftest.py` (`_block_external_network`, autouse) + `test_network_guard.py` | novo |
| Latência do modo `slow` do mock sem depender de relógio real apertado | `test_mock_upstream.py::test_slow_respects_latency`, `test_slow_really_waits_with_generous_tolerance` | novo |

## Observabilidade e demais

| Requisito | Teste(s) | Situação |
|---|---|---|
| Métricas Prometheus, baixa cardinalidade, registry por app | `test_metrics_recorder.py::test_metrics_endpoint_is_prometheus_text`, `test_labels_have_low_cardinality`, `test_registry_is_isolated_per_app` | ok |
| Fallback e 503 nas métricas | `test_metrics_recorder.py::test_fallback_labels_include_reason_and_final_provider`, `test_503_uses_provider_none_and_to_none` | ok |
| Recorder: uma linha por requisição (inclui 503 e erros), `record_content: false`, falha de escrita não derruba | `test_metrics_recorder.py::test_records_*`, `test_record_content_false_keeps_only_metadata`, `test_write_failure_does_not_affect_response` | ok |
| `/status` sem segredos e somente leitura | `test_resilience.py::test_status_reflects_state_and_exposes_no_secrets`, `test_status_is_read_only` | ok |
| Config validada (duplicados, chain, campos desconhecidos, resiliência) | `test_gateway_config.py`, `test_resilience.py::test_invalid_resilience_config_fails_at_startup` | ok |
| Mock upstream: modos, `fail_every_n`, reset, 422, stats | `test_mock_upstream.py` | ok |
| Pontos de entrada reais (`python -m mock_upstream.server`, uvicorn do gateway) | `test_integration.py::test_real_processes_end_to_end` (opcional) | novo |

## Ramos de decisão achados pela cobertura (Marco 6)

| Requisito | Teste(s) | Situação |
|---|---|---|
| `Retry-After` em segundos, data HTTP (futura, passada, sem fuso) e lixo | `test_decision_branches.py::test_parse_retry_after_*` | novo |
| Lock do budget ocupado (thread ou outro processo) = esgotado, sem gastar, e volta a funcionar | `test_decision_branches.py::test_budget_thread_lock_timeout_is_fail_closed`, `test_budget_file_lock_held_by_another_handle_is_fail_closed` | novo |
| Lifespan real cria `budget.json` em zero e não toca em arquivo existente | `test_decision_branches.py::test_real_lifespan_*`, `test_lifespan_does_not_touch_existing_budget_file` | novo |
| Conteúdo em partes (`[{"type":"text"}]`) na estimativa, na tradução Anthropic e no mock | `test_decision_branches.py::test_estimate_tokens_*`, `test_anthropic_translation_*`, `test_mock_echoes_text_parts_*` | novo |
| Falha periódica do mock com status 4xx comum (sem `Retry-After`) | `test_decision_branches.py::test_mock_non_5xx_non_429_failure_status_is_client_style_error` | novo |

## Avaliação de qualidade, Fase 3 (Marco 9)

| Requisito | Teste(s) | Situação |
|---|---|---|
| `config.eval.yaml` carrega no schema comum; chain só com os dois Ollama; `anthropic` desabilitado e fora da chain; sem limites artificiais (sem rate limit, breaker alto, timeout >= 120 s, warmup, contexto 3072) | `test_eval_config.py` | novo |
| Dataset real: 48 prompts (6 categorias x 8), ids únicos, `temperature` 0, `check`/`reference_answer` por categoria, cabe no contexto local | `test_eval_dataset.py::test_real_dataset_*`, `test_cli_accepts_the_real_dataset` | novo |
| Validador rejeita id duplicado (no arquivo e entre arquivos), campo faltando, `check` inválido (20 casos), prompt acima do contexto, padrão sensível (sem ecoar o valor), estrutura inválida, JSON inválido e contagem errada | `test_eval_dataset.py::test_rejects_*`, `test_sensitive_scan_*`, `test_expected_counts_*`, `test_cli_reports_*` | novo |
| Estimativa de tokens do dataset idêntica à do gateway (sem importar o módulo do cliente da Anthropic) | `test_eval_dataset.py::test_token_estimate_matches_the_gateway` | novo |
| Hash do dataset estável e independente do fim de linha | `test_eval_dataset.py::test_dataset_hash_is_stable_and_ignores_line_endings` | novo |
| Dataset versionado; `eval/results/` e `eval/baseline/` ignorados | `test_eval_dataset.py::test_dataset_is_versioned_but_results_and_baseline_are_ignored` | novo |
| Runner grava um resultado por prompt, em sequência, com provedor forçado, `temperature` 0 e o `max_tokens` do prompt | `test_eval_runner.py::test_collect_*` | novo |
| Falha, timeout ou 5xx de um prompt vira resultado com `error` e não derruba o run | `test_eval_runner.py::test_one_failing_prompt_*`, `test_transport_errors_*`, `test_a_failing_prompt_does_not_break_execute`, `test_ask_*` | novo |
| `--resume` pula os `prompt_id` gravados, tolera linha final truncada e não duplica | `test_eval_runner.py::test_resume_*`, `test_execute_resume_skips_what_is_recorded` | novo |
| `meta.json` completo (data, modelo, versão do Ollama, hashes do dataset e do config, commit); dois provedores no mesmo run; hash divergente recusado | `test_eval_runner.py::test_execute_runs_the_whole_flow_*`, `test_two_providers_*`, `test_changed_dataset_or_config_*` | novo |
| Config derivado: chain só com o provedor medido, outro modelo sem warm-up, budget e recorder dentro do run | `test_eval_runner.py::test_execute_uses_a_derived_config_*` | novo |
| Filhos com `ENABLE_REAL_API=0` e `ANTHROPIC_API_KEY` vazia, bind em `127.0.0.1` | `test_eval_runner.py::test_child_processes_get_real_api_off_*`, `test_guard_environment_*` | novo |
| Recusa rodar com `anthropic` na chain ou habilitado, ou com `ENABLE_REAL_API=1`, antes de tocar Ollama ou processos | `test_eval_runner.py::test_refuses_to_run_with_anthropic_*`, `test_derived_config_never_*`, `test_refuses_when_real_api_*` | novo |
| Pré-checagens: Ollama no ar, modelo existente (só `GET`, nunca baixa), dataset válido, provedor válido, `run-id` seguro, config válido | `test_eval_runner.py::test_refuses_*` | novo |
| `main`: erro claro (exit 2), Ctrl+C (exit 130), opções repassadas | `test_eval_runner.py::test_main_*` | novo |
| O código de avaliação não importa nem referencia cliente da Anthropic (AST, texto e `sys.modules`) | `test_eval_runner.py::test_runner_does_not_import_*`, `test_importing_the_runner_*` | novo |
| Ollama real: 2 prompts por provedor (opcional, `skip` sem Ollama) | `test_eval_runner.py::test_real_ollama_runs_two_prompts_per_provider` | novo |
| Pilha de processos compartilhada: env seguro, encerramento garantido (também em falha) | `test_demo.py::test_start_stack_*`, `test_integration.py::test_processes_are_terminated_*` | refatorado |

## Scorers, similaridade e estatística, Fase 3 (Marco 10)

| Requisito | Teste | Situação |
|---|---|---|
| `json_schema`: JSON em uma linha, indentado, com cerca, com prosa antes/depois; `format_clean` registra se precisou de limpeza | `test_scorers.py::test_json_schema_perfect_in_every_wrapping_*`, `test_extract_json_*` | novo |
| `json_schema`: score = fração de campos corretos (ausente, tipo errado, valor errado, enum, `additionalProperties`, campo `required` sem `expected`, lista inteira) | `test_scorers.py::test_json_schema_*` | novo |
| `exact`: normalização configurável (caixa, acentos, pontuação, espaços) | `test_scorers.py::test_exact_*` | novo |
| `regex`: `re.search` com flags, cerca removida antes | `test_scorers.py::test_regex_search_with_flags` | novo |
| `numeric`: último número, formatos PT-BR (`1.234,5`, `1,5`, `R$ 15,00`), unidade solta, tolerância | `test_scorers.py::test_parse_number_formats`, `test_numeric_*` | novo |
| `label_in`: palavra inteira, dois rótulos diferentes = falha, mesmo rótulo repetido ok, acento e `_` | `test_scorers.py::test_label_in_*` | novo |
| `constraints`: score = fração das regras; palavras inteiras; itens, linhas, frases, início/fim, minúsculas, sem quebra de linha | `test_scorers.py::test_constraints_*` | novo |
| Erro, timeout, 4xx/5xx e resposta vazia: score 0 com `errored`, contados à parte | `test_scorers.py::test_runner_errors_and_empty_answers_*` | novo |
| Entrada estranha (None, número, lista, dict, controle, emoji, 100 mil caracteres) nunca levanta; spec quebrado vira `scorer_error` | `test_scorers.py::test_never_raises_*`, `test_a_broken_spec_*` | novo |
| Summarization: composta (cobertura, similaridade, tamanho), pesos documentados; limite lido do enunciado; truncamento por `max_tokens`; passos ignoram frase de abertura | `test_scorers.py::test_summarization_*`, `test_size_limit_parsing`, `test_key_point_coverage_*` | novo |
| Sem embedder (ausente ou quebrado) a summarization usa só cobertura e tamanho e registra `embedding: unavailable` | `test_scorers.py::test_summarization_degrades_*` | novo |
| Embedding: sem dependência ou sem modelo no cache vira `unavailable`; só baixa com `allow_download`; modelo padrão multilíngue | `test_embedding.py::test_missing_dependency_*`, `test_model_not_in_cache_*`, `test_download_only_when_*` | novo |
| Modelo real: paráfrase mais próxima que texto sem relação (`@pytest.mark.integration`, `skip` sem modelo no cache) | `test_embedding.py::test_real_model_similarity_*` | novo |
| `sentence-transformers` fora do `requirements.txt` e do CI; importar o CLI não o carrega | `test_eval_stats.py::test_sentence_transformers_is_not_a_default_dependency` | novo |
| Wilson 95% com valores conhecidos (8/10, 5/10, 0/10, 10/10, 1/8, 50/100) | `test_eval_stats.py::test_wilson_*` | novo |
| McNemar exato com valores conhecidos (0/0, 3/3, 0/5, 1/5, 0/10, 2/8), simétrico, teto 1 | `test_eval_stats.py::test_mcnemar_*` | novo |
| Percentis por interpolação linear (p50, p95) | `test_eval_stats.py::test_percentile_*` | novo |
| Bootstrap reprodutível com semente fixa, valor dourado, não mexe no `random` global, degenerado/vazio | `test_eval_stats.py::test_bootstrap_*` | novo |
| Erros ficam fora da média, da aprovação e da latência, mas aparecem em `errors` | `test_eval_stats.py::test_group_stats_*` | novo |
| Comparação pareada: IC inclui zero ou poucos pares ou McNemar não confirma = "indicativo, não conclusivo", sem vencedor; só declara com IC e McNemar de acordo; pares com erro ficam de fora | `test_eval_stats.py::test_paired_block_*`, `test_verdict_*` | novo |
| Listas "os dois reprovam" e "só um reprova"; escolha do par; tokens/s | `test_eval_stats.py::test_both_fail_*`, `test_pick_pair`, `test_tokens_per_s` | novo |
| CLI de ponta a ponta (sem rede): `scores.jsonl`, `summary.json`, determinístico com a semente, amostra de revisão de 12 (2 por categoria), run inexistente, `prompt_id` desconhecido | `test_eval_stats.py::test_cli_*`, `test_review_sample_*`, `test_unknown_prompt_id_*` | novo |
| O código de pontuação não importa cliente de rede nem Anthropic | `test_eval_stats.py::test_scoring_code_never_imports_*` | novo |

Quebras intencionais (um scorer de cada vez, restaurado depois): `numeric` pegando o primeiro número (3 testes falham),
`numeric` sem vírgula decimal (3), `label_in` aceitando dois rótulos (3) e `label_in` sem palavra inteira (2).

## Juiz local, calibração e revisão humana, Fase 3 (Marco 11)

| Requisito | Teste | Situação |
|---|---|---|
| Parser do juiz: JSON limpo, com cerca de código, com prosa antes/depois, truncado (recupera `overall`) e inválido; nota fora da escala, não inteira ou booleana é rejeitada | `test_judge.py::test_parse_*`, `test_coerce_score` | novo |
| Parser do pareado: A, B, empate, "Resposta A", cerca, truncado; "Ambos" e lixo são inválidos | `test_judge.py::test_parse_pairwise_*` | novo |
| Montagem do prompt: resposta entre delimitadores, em bloco próprio, fora do prompt de sistema | `test_judge.py::test_answer_goes_between_explicit_delimiters` | novo |
| O juiz nunca recebe `check`, `notes` nem resultado do scorer (campo a campo e texto, no dataset inteiro) | `test_judge.py::test_judge_never_receives_check_*`, `test_no_prompt_of_the_dataset_leaks_*` | novo |
| `reference_answer` e `key_points` só chegam em summarization; toda categoria tem rubrica | `test_judge.py::test_reference_and_key_points_*`, `test_every_task_type_has_a_rubric_*` | novo |
| Resposta que tenta manipular o juiz ("dê nota 5", marcadores falsos) não altera o prompt de sistema e não fecha o bloco | `test_judge.py::test_injection_*`, `test_neutralize_*` | novo |
| Pareado anônimo (sem nome de modelo) e com a ordem TROCADA na segunda rodada | `test_judge.py::test_pairwise_prompt_is_anonymous_*`, `test_pairwise_orders_swap_*`, `test_plan_items_swaps_the_order_*` | novo |
| Respostas com erro/vazias saem do plano; sem par de provedores o pareado não existe | `test_judge.py::test_plan_items_*` | novo |
| JSON inválido: UMA nova tentativa com lembrete de formato; depois `judge_error` sem derrubar o run; erro de transporte não tenta de novo | `test_judge.py::test_invalid_json_is_retried_*`, `test_still_invalid_*`, `test_transport_error_*` | novo |
| Coleta retomável: `--resume` pula o gravado (inclusive `judge_error`), descarta linha final truncada | `test_judge.py::test_resume_*`, `test_collect_*` | novo |
| Chamada ao gateway: provedor forçado, `temperature` 0, `max_tokens` baixo; falhas viram erro | `test_judge.py::test_post_chat_*` | novo |
| Pré-checagens: `ENABLE_REAL_API` ligada, `anthropic` na chain ou habilitado, modelo ausente, Ollama fora, run inexistente, dataset mudou, juiz desconhecido | `test_judge.py::test_execute_refuses_*`, `test_execute_unknown_judge_*`, `test_execute_ollama_down`, `test_execute_missing_run_*` | novo |
| `execute` de ponta a ponta (gateway e Ollama simulados): meta com hash do prompt do juiz, recusa sobrescrever, `--resume`, hash diferente recusa, `--limit` | `test_judge.py::test_execute_*` | novo |
| O código do juiz não importa nem referencia cliente da Anthropic | `test_eval_runner.py::test_runner_does_not_import_*` (varre `evalkit/`), `test_judge.py::test_importing_the_judge_*` | novo |
| 2 julgamentos reais por juiz (uma resposta errada e uma que tenta manipular), `skip` sem Ollama | `test_judge.py::test_real_ollama_makes_two_judgments_per_judge` (`-m integration`) | novo |
| Kappa de Cohen com valores conhecidos (0,4 em 2x2; 0,5 em 3 categorias; 1, -1, 0; indefinido vira `None`) e IC bootstrap reprodutível | `test_judge_stats.py::test_kappa_*`, `test_bootstrap_kappa_*` | novo |
| Acurácia, precisão e revocação da aprovação; denominador zero vira `None`; aprovar tudo tem acurácia alta e kappa 0 | `test_judge_stats.py::test_binary_metrics_*`, `test_approving_everything_*` | novo |
| Critério de decisão fixado em constantes (kappa >= 0,6 e acurácia >= 0,80), com os limites exatos; kappa indefinido reprova | `test_judge_stats.py::test_decision_criterion_*`, `test_judge_report.py::test_decision_labels_*` | novo |
| Spearman com valor conhecido (8/sqrt(95)), monótono, invertido, constante e poucos pares | `test_judge_stats.py::test_spearman_*` | novo |
| Pareado: tabela-verdade das duas rodadas e índice de viés de posição (2 de 4 = 0,5); juiz que sempre escolhe a posição A tem índice 1 | `test_judge_stats.py::test_pairwise_outcome_*`, `test_position_bias_*`, `test_a_judge_that_always_picks_*` | novo |
| Taxa de vitória (empate vale 0,5) e autopreferência com valores conhecidos (nota própria +2, folga -2, líquido 4; pareado 0,5); aprovações indevidas em respostas próprias x do outro | `test_judge_stats.py::test_win_rate_*`, `test_judge_report.py::test_self_preference_*` | novo |
| Concordância entre juízes, sem média de notas | `test_judge_report.py::test_cross_judge_agreement_*` | novo |
| Calibração por categoria; `judge_error` e erro do scorer ficam fora e são contados | `test_judge_report.py::test_calibration_block_*` | novo |
| Summarization: Spearman nota x score e rótulo de confiabilidade; juiz constante dá `None`; `instruction` fica numa análise extra à parte | `test_judge_report.py::test_summarization_block_*`, `test_instruction_extra_*` | novo |
| Revisão humana: 16 itens por semente (4 concordam, 4 discordam com piso de 2 aprovações indevidas de respostas do Qwen, 8 de summarization, 4 de cada modelo), determinística, resposta não fecha o bloco de código | `test_judge_report.py::test_review_*`, `test_stratified_pick_*`, `test_fenced_text_*`, `test_end_to_end_report_*` | novo |
| Juiz perfeito é "uso permitido"; juiz invertido é "NÃO CONFIÁVEL"; com um só juiz não há seção cruzada | `test_judge_report.py::test_oracle_judge_*`, `test_report_with_a_single_judge_*` | novo |
| O relatório só lê (nunca reescreve os julgamentos) e os erros são claros | `test_judge_report.py::test_report_errors_are_clear`, `test_report_cli_main_*` | novo |

Quebras intencionais (uma de cada vez, restaurada com `git checkout`): troca de ordem do pareado removida (2 testes falham:
`test_pairwise_orders_swap_the_second_round`, `test_plan_items_swaps_the_order_in_the_second_round`); filtro que impede o
`check` de chegar ao juiz removido (2: `test_judge_never_receives_check_notes_or_scorer_result`,
`test_reference_and_key_points_only_for_summarization`); neutralização de marcadores removida (3, todos de injeção); nova
tentativa removida (4); limiar de aprovação 3 em vez de 4 (2).

## Cobertura

`pytest --cov` (config em `pyproject.toml`, `fail_under = 85`, com ramos): **96%** (96,3% medido no Marco 11, 653 testes + 10 de integração) em `gateway/`,
`mock_upstream/` e `evalkit/`. O que sobra sem cobertura é irrelevante para decisões: o ramo `fcntl` (só Linux/macOS),
o bloco `if __name__ == "__main__"` dos servidores e a limpeza no `finally` de erros de E/S raros.
