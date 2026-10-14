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
| Latência do modo `slow` do mock sem depender de relógio real apertado | `test_mock_upstream.py::test_slow_respects_latency`, `test_slow_waits_requested_latency` | novo |

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
