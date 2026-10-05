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

## Activity interceptor（`tests/unit/test_log_activity_interceptor.py`、unit + time-skipping server）

| テスト | 守るもの |
|---|---|
| `test_every_activity_input_type_is_in_the_explicit_table` / `test_the_table_only_names_attributes_that_exist` | 文脈は入力型ごとの明示の表から。新しい Activity を足して表を忘れると、そのログに episode_id が付かないまま気付けない |
| `test_research_request_id_is_not_the_api_request_id` | Research の `request_id`（依頼 ID）を API の `request_id` に入れない（照合を誤らせる名前衝突） |
| `test_voice_scene_id_is_the_script_scene_id` | 音声の `scene_id` は台本のシーン ID（音声 Artifact と同じ名前空間）。storyboard 側は attributes |
| `test_unknown_input_types_bind_nothing` | 属性名で汎用的に拾わない |
| `test_success_binds_context_and_records_duration` | Activity 内の任意の logger に activity info と入力の文脈が付く |
| `test_failure_is_recorded_and_the_same_object_is_reraised` / `test_plain_exceptions_are_retryable_by_temporal` | 失敗の `error_type`（ドメイン名）・`failure_class`・`retryable`（Temporal の再試行）。**同じ例外オブジェクト**を再送出（INV-38: 例外の型・中身を変えない） |
| `test_cancel_is_recorded_as_cancelled_and_propagates` | 兄弟 Activity の cancel を失敗と区別する（`outcome=cancelled`） |
| `test_a_broken_logger_does_not_change_the_activity_outcome` | ロガーが壊れても Activity の結果・例外は同じ（INV-38） |
| `test_two_episodes_in_parallel_do_not_mix` | 実 Worker で2つの Episode を並列に走らせても文脈が混ざらない |

## 接続（`tests/unit/test_log_wiring.py`、`tests/unit/test_log_api.py`、unit）

| テスト | 守るもの |
|---|---|
| `test_worker_entry_emits_service_started_and_stopped` / `test_worker_entry_emits_start_failed_for_an_early_crash` | 起動・停止・起動失敗のイベント。既存の文言（`starting revision=`）は変えない |
| `test_cli_configures_logging_once_before_running` | 初期化は起動点の1か所（各 worker の `basicConfig` を消したので、ここが抜けるとログが一切出ない） |
| `test_api_serve_uses_the_common_setup_and_no_uvicorn_logging` | API は uvicorn の既定のログ設定（stderr・独自書式・access log）を使わない |
| `test_core_forwarding_is_only_installed_after_configure` / `test_connect_installs_forwarding_before_the_first_connect` | Core の転送は最初の接続より前。logging を設定しないプロセス（テスト・スクリプト）では既定の Runtime を変えない |
| `test_worker_interceptors_is_the_single_entry` | 本番とテストの Worker が同じ入口から interceptor を取る |
| `test_health_is_logged_at_debug` | 死活監視の叩く `/healthz` を INFO で溢れさせない |
| `test_route_template_episode_id_and_request_id` | route は template（実 path・query を出さない）、path の `episode_id`、`X-Request-ID` の引き継ぎ、resume の requested→started |
| `test_rejected_resume_is_logged_with_the_status` / `test_already_running_resume_is_rejected_with_409` | resume の拒否（404・409・二重起動）を status つきで残す |
| `test_unsafe_request_ids_are_replaced` | 受信ヘッダの任意の文字列を keyword に入れない |
| `test_an_exception_is_logged_and_reraised_unchanged` | middleware は例外を記録して同じオブジェクトを再送出（応答・例外を変えない） |
| `test_lifespan_logs_service_started_and_stopped` | API の起動・停止 |

## 境界（`tests/architecture/test_logging_boundaries.py`）

| テスト | 守るもの |
|---|---|
| `test_every_worker_gets_the_activity_logging_interceptor` | 全 `run_worker.py` の `Worker(...)` が `interceptors=worker_interceptors()`。付け忘れた worker の Activity は文脈なしになる |
| `test_logging_is_configured_only_at_the_entry_points` | `basicConfig`・httpx のレベル設定を各 worker に戻さない（二重の handler、query 付き URL の INFO） |
| `test_workflow_modules_do_not_import_infrastructure` | INV-40。sandbox 内で `infrastructure.logging` が再 import されると handler が分裂し workflow task が失敗する（実測） |
| `test_nothing_imports_opensearch` | アプリは stdout にしか書かない（ADR-0040 §1。OpenSearch の停止が業務に届かない） |
| `test_log_extra_uses_only_the_avp_key` | `extra=` のキーは `"avp"` だけ。予約属性と衝突すると `makeRecord` が KeyError を投げ、業務へ伝播する |

## 故障注入（`tests/unit/test_log_fault_injection.py`）— INV-38

| テスト | 守るもの |
|---|---|
| `test_the_fault_injection_really_breaks_emission` | 注入（`tests/support/json_log_plugin.break_logging`）が実際に発行を壊していること。効いていなければ次の検査は空振り |
| `test_ledger_suites_pass_unchanged_with_broken_logging` | 既存の台帳・有料 submit/await・fal adapter のテスト群を**書き換えずに**、ロガーを壊した状態で全部通す。期待値（台帳の状態・例外の型）は既存テストが持つ |

## commit 後のイベント（`tests/unit/test_log_ledger.py`、unit: SQLite）— log-contract §9

予約台帳の書き手は5か所（paid_job・upload・planning・storyboard・scene_recovery）あるので、発行は
repository のメソッドが session に積み、`after_commit` で出す（`infrastructure/logging/ledger.py`）。

| テスト | 守るもの |
|---|---|
| `test_reservation_events_come_only_after_commit` | flush しただけでは出ず、commit の後に `reservation_id`・`provider_attempt`（台帳ラウンド）つきで出る |
| `test_rolled_back_or_uncommitted_changes_are_not_logged` | rollback・commit せずに閉じた変更は出さない。別の session の commit で古い保留が漏れない（「ログにある＝DB にある」） |
| `test_dispatch_job_ref_and_spent_follow_the_ledger` | dispatched → job_ref_recorded → spent の順。同じ参照の再記録（no-op）では出さない |
| `test_artifact_stored_and_superseded` | 新しい世代の記録で旧世代の superseded と新世代の stored。同じ内容の再記録は何も出さない |
| `test_rejection_and_auth_incident` | 拒否は `error_code`（観測）と `error_category`（`RejectionCategory` の値）を分けて出す。403 は `access_denied` / `http_status_only`（credentials と断定しない） |

## 有料ジョブ（`tests/unit/test_log_paid_job.py`、unit: SQLite + fake generator）

| テスト | 守るもの |
|---|---|
| `test_a_successful_round_reads_in_ledger_order` | reserved → dispatched → submit 受理（ref の commit **前**）→ job_ref_recorded → poll の状態変化 → spent の順で、全てに episode・scene・provider が付く。`provider_attempt` は台帳ラウンド、run ごとの試行番号は `attributes.run_attempt`（2つの「試行」を混ぜない） |
| `test_resume_and_reuse_are_visible` | 既存の予約の再開（再 submit しない）がログで見える |
| `test_failed_job_spends_conservatively_and_is_logged` | provider のジョブ失敗は conservative の spent として残る |
| `test_an_unreconciled_reservation_blocks_and_is_logged` | 未照合の予約が新ラウンドを止めた判断（`reservation.blocked`）を、止めた予約の ID つきで残す |
