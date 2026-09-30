# 構造化ログ（ADR-0040 / INV-38〜40）のテスト設計根拠

ログは検索用の副本で、業務の正は DB と Temporal。だからここのテストが守るのは「ログが正しい」ことより、
**ログが業務を壊さない（INV-38）・秘密を出さない（INV-39）・Workflow の決定性を崩さない（INV-40）**こと、
そして B（mapping 生成）と C（OpenSearch での検証）が前提にする**形式の契約**。

## 整形器（`tests/unit/test_log_formatter.py`、unit）

| テスト | 守るもの / 落ちたら何が起きているか |
|---|---|
| `test_plain_record_has_every_required_field` | `REQUIRED_APP_FIELDS` が全行に付き、型が mapping 型に合う。欠けると Dashboards の絞り込みから行が消える |
| `test_unknown_identity_values_are_marked_unknown` | env が無い・語彙外なら `unknown`（空文字・推測値で埋めない / log-contract §2） |
| `test_event_fields_come_from_the_single_avp_extra_key` | 発行側の値は `extra={"avp": ...}` から型どおりに出る。契約外・Collector のフィールドはトップレベルに出ない（`dynamic: false` の mapping と食い違わない） |
| `test_values_of_the_wrong_type_are_moved_to_attributes` | 型の合わない値を捨てずに `attributes` へ。boolean/keyword は OpenSearch で `ignore_malformed` できないので、トップレベルに出すと bulk が 400 になる |
| `test_emitter_cannot_override_the_fields_the_formatter_owns` | `service_name` や `event_id` を発行側が偽れない（版混在の確認・重複除去が壊れる） |
| `test_context_is_attached_and_the_emitter_wins` / `test_temporal_activity_info_is_mapped_to_contract_names` | 文脈と SDK の extra（`attempt`→`activity_attempt`、`workflow_run_id`→`run_id`）の写し。名前を間違えると Temporal との照合ができない |
| `test_keyword_values_are_cut_at_the_mapping_limit` / `test_message_is_cut_by_utf8_bytes` / `test_response_excerpt_is_json_and_its_cut_is_flagged` / `test_attributes_are_bounded` | 各上限と切り詰めの事実（`*_truncated`）。ignore_above を超えた keyword は黙って検索不能になるので、発行側で切って印を付ける |
| `test_the_whole_line_never_exceeds_the_event_limit` | 1行が `EVENT_MAX_BYTES` 以下。超えると Docker が partial に分割し、Collector が JSON として読めない |
| `test_stack_keeps_the_root_cause_of_a_long_chain` / `test_deep_stacks_keep_the_first_and_last_frames` / `test_exception_groups_and_notes_are_kept` | stack は chain の各例外の先頭・末尾を残す。末尾だけを残すと根本原因（403 の元になった httpx の例外など）が消える |
| `test_application_error_type_is_the_domain_name` | Activity 失敗の `error_type` はドメインの例外名。`ApplicationError` だけでは分類できない |
| `test_a_broken_record_falls_back_to_the_fixed_minimal_form` | 整形の失敗は例外にせず固定形で出し直す（INV-38） |
| `test_levels_map_to_the_contract_vocabulary` | 独自レベルも `LogLevel` の語彙に丸める |
| `test_workflow_event_id_is_deterministic_and_every_input_matters` / `test_workflow_event_id_does_not_collide_across_a_grid` | uuid5 の入力が全部効き、衝突しない。衝突すると OpenSearch の `create` が後の文書を黙って捨てる（log-contract §4） |
| `test_outside_a_workflow_the_id_is_random_even_with_workflow_info` | Workflow スレッドの外では uuid4（同じ ID を別の記録に付けない） |
| `test_seq_counts_per_run_and_history_length` | seq は `(run_id, history_length)` ごと、上限つきで忘れる（メモリが増え続けない） |

## 安全化（`tests/unit/test_log_redaction.py`、unit）— INV-39

| テスト | 守るもの |
|---|---|
| `test_secret_key_names` / `test_ordinary_key_names` | log-contract §7.2 のキー名規則。`episode_id` 等を誤って伏せない |
| `test_value_patterns_are_replaced` | §7.3 の値のパターン（Bearer・fal key・JWT・private key・DSN・Google token・`sk-`・`key=value`・SQLAlchemy の `[parameters: …]`・長い base64） |
| `test_allowed_hosts_are_derived_from_the_adapter_constants` | 許可 host は adapter の定数から導く（写しを持たない / AGENTS §8）。capability URL の host（`v3.fal.media`）は含まない |
| `test_allowed_host_keeps_path_but_drops_query_and_userinfo` / `test_other_hosts_are_shrunk_to_a_hash` | URL の query（YouTube の `upload_id` 等）を落とし、署名つき URL の path を縮約する |
| `test_sanitize_is_idempotent_on_already_redacted_text` | 既存の adapter の伏せ字処理の出力に重ねても壊れない（ADR-0040 §3） |
| `test_message_attributes_and_exception_text_are_cleaned` | message・attributes・response_excerpt・例外 chain の文字列の全部を通す |
| `test_third_party_logger_goes_through_the_same_formatter` | 第三者 logger（sqlalchemy・httpx）も同じ整形器。httpx の INFO は出さない |
| `test_uncaught_exception_goes_through_the_formatter` / `test_warnings_are_captured` | 未捕捉例外と `warnings` が stderr に素で出ない |
| `test_configure_logging_does_not_stack_handlers` | 何度呼んでも handler は1つ（二重出力・Workflow の sandbox での再登録を防ぐ） |
| `test_text_format_is_available_for_rollback_and_still_sanitizes` | `AVP_LOG_FORMAT=text` で従来形式に戻せ、それでも安全化される |
| `test_temporal_core_logs_are_forwarded_not_written_to_stderr` | Temporal Core（Rust）の既定は console へ非 JSON で直書き（実測では stdout に ANSI 付き）。転送を置くと全行が JSON になる。子プロセスで実際の Core のログを出して確かめる |

## 発行ヘルパーと文脈（`tests/unit/test_log_emit.py`、unit）

| テスト | 守るもの |
|---|---|
| `test_emit_puts_fields_under_the_single_extra_key` | `message`・`name` 等の予約属性と同名のフィールドでも `makeRecord` が KeyError を投げない（`raiseExceptions=False` でも業務へ伝播する経路） |
| `test_emit_never_raises_even_when_the_logger_is_broken` | ロガーの故障が業務の例外にならない（INV-38） |
| `test_emit_respects_the_level` | DEBUG のイベント（poll 等）を INFO 運用で出さない |
| `test_context_is_restored_on_exception` / `test_parallel_tasks_do_not_see_each_others_context` | 文脈は例外でも戻り、同時に走る2つの Episode で混ざらない |
