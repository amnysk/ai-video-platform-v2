# 未照合予約 waiver（ADR-0062 / INV-85）のテスト設計根拠

要件: 予算 0 の間、台帳を書かずに 1 件・期限つきで release を通せる。それ以外（2 件目、期限切れ、
別の request、予算が 0 でない、実行中の有料作業）では必ず BLOCKED。fake の collector だけで、DB・fal には届かない。

| テスト | 守るもの / 落ちたら何が起きているか |
|---|---|
| `test_single_target_reservation_with_all_conditions_is_waived_and_ready` | 全条件が揃うと WAIVED・総合 READY。生 token が出力に出ない。ここが落ちると例外が使えない / token が漏れる |
| `test_explicit_zero_budget_is_accepted` | `0` / `0.00` はどちらも明示的な 0 |
| `test_count_zero_ignores_the_waiver` | 件数 0 は従来どおり PASS。waiver の有無が結果を変えない |
| `test_no_waiver_file_keeps_the_original_blocked_detail` | waiver が無い経路は従来の BLOCKED と同じ detail |
| `test_a_second_reserved_row_blocks` | waiver は新しい予約を覆わない。落ちると別の未照合予約が黙って通る |
| `test_count_changing_during_the_read_blocks` | 件数の読みと行の読みの間に増えたら BLOCKED（競合） |
| `test_expired_waiver_blocks` | 期限切れの waiver は無効 |
| `test_window_over_72_hours_blocks` | 期限の幅の上限（長期の白紙委任を防ぐ） |
| `test_expiry_not_after_approval_blocks` | 期限が承認より前 / 同時は無効 |
| `test_approved_in_the_future_blocks` | 未来日付の承認（時計ずれ許容を超える）は無効 |
| `test_naive_times_block` | タイムゾーンなしの時刻は比較できないので無効 |
| `test_request_id_mismatch_blocks` | 別の provider request の waiver を流用できない |
| `test_reservation_id_mismatch_blocks` | 別の予約行の waiver を流用できない |
| `test_wrong_sha_and_wrong_kind_block` | 別 SHA / 別種のファイルの流用を防ぐ |
| `test_unparseable_waiver_is_blocked_not_unknown` | 壊れた waiver は例外の要求なので BLOCKED（UNKNOWN にしない） |
| `test_fingerprint_mismatch_blocks_when_the_row_changed` | waiver 作成後に行が変わった（照合された等）ら再確認を強制 |
| `test_row_that_is_not_reserved_blocks` | `reserved` 以外の行は対象外 |
| `test_only_an_explicit_zero_budget_is_accepted` | None・空・正・負・非数・NaN は全て BLOCKED。未設定を 0 と読まない |
| `test_waiver_must_require_zero_budget` | waiver 自体が予算 0 を前提と宣言している |
| `test_missing_approver_bad_token_or_reason_blocks` | 承認者・承認参照の形式・理由が必須 |
| `test_running_workflows_block` | 実行中の有料作業があれば免除しない |
| `test_evidence_hash_mismatch_or_missing_blocks` | 証跡の欠落・改変・空リスト・型違いは BLOCKED |
| `test_collector_failure_is_unknown` | collector の失敗は従来どおり UNKNOWN（`_guard`） |
| `test_waived_on_a_non_waivable_check_is_blocked` | WAIVED は他のチェックでは総合 BLOCKED |
| `test_waived_counts_as_satisfied_only_for_reservations` | WAIVED が必須を満たすのは `WAIVABLE_CHECKS` だけ |
| `test_approved_report_accepts_waived_reservations_only` | 承認済みレポートは reservations の WAIVED だけ許し、他の WAIVED は承認を BLOCKED にする |
| `test_other_checks_are_identical_with_and_without_the_waiver_file` | waiver が他のチェックの結果を変えない |
| `test_text_and_json_show_waived_with_a_warning` | WAIVED と警告が text / JSON に出る |
| `test_default_waiver_path_and_fingerprint_are_stable` | 既定パスと指紋が決定的（所有者が同じ値を計算できる） |

`tests/architecture/test_release_preflight_read_only.py`: 新しい SELECT（`pending_reservations`）が許可リスト内の読み取りに留まる。

## 独立レビュー側の追加（`test_release_budget_setting_source.py` / `test_release_reservation_waiver_safety.py`）

| テスト | 存在理由 |
|---|---|
| `test_dotenv_value_is_the_source` | 予算の出所は deploy が適用する repo の `.env` |
| `test_process_env_alone_is_not_the_source` | `.env` に無く環境変数だけの 0 は採らない（UNKNOWN） |
| `test_matching_process_env_is_accepted_after_quote_handling` | 同じ値（引用符・空白を除く）なら通す |
| `test_process_env_that_differs_from_dotenv_is_unavailable` | 環境変数と `.env` が食い違うと、どちらが効くか不明なので READY にしない |
| `test_production_shaped_reservation_is_waived` | 本番で実際に残っている予約の形（fal_video、dispatched 済み、raw/outcome なし）で成立する |
| `test_production_shaped_reservation_is_not_waived_for_a_different_request_id` | その形でも request id が違えば免除しない |
| `test_collectors_protocol_has_no_write_shaped_method` | collector の面に書き込み系の動詞が無い（台帳に触れない） |
| `test_a_waived_run_calls_only_protocol_read_methods` | WAIVED の実行が呼ぶのは Protocol の読み取りだけ |
| `test_the_new_collector_sql_is_one_select_and_collectors_are_in_the_read_only_scan` | 新 SQL は単一の SELECT で、read-only 走査の対象に collectors.py が入っている |
| `test_waiver_requires_a_budget_that_forbids_submit` | waiver は submit 不可の予算（0）を前提にする。`evaluate_budget(0)` は不許可 |
| `test_the_resubmit_guards_that_the_waiver_relies_on_still_exist` | 再送禁止を担う既存テストが消えたら落ちる（複製せず存在を固定） |
| `test_a_valid_waiver_does_not_relax_any_other_check` | 他チェックを 1 項目ずつ壊しても、waiver の有無で結果が同一かつ READY にならない |
| `test_the_fault_matrix_covers_several_distinct_checks` | 上のマトリクスが形だけで終わらず複数のチェックを実際に壊している |
| `test_waiver_is_ignored_in_post_deploy_mode` | deploy 後検証に waiver は持ち越されない |
| `test_a_waiver_bound_to_sha_a_does_not_apply_to_sha_b` | SHA 束縛 |
| `test_the_raw_owner_token_never_appears_in_any_output` | 生の承認 token は text / JSON に出ない |
