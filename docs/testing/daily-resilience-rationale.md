# テスト設計の根拠: 日次の事前診断・保留・watchdog 分類（ADR-0058）

背景: 2026-10 の日次 7 日連続失敗（Codex の model 不一致）と、fal の 403（残高 / 鍵が未確定）。
どのテストも本物の codex・fal・YouTube を呼ばない（AGENTS.md §9）。

## Codex model の事前診断（INV-75）

| テスト | 止めるもの |
|---|---|
| `tests/unit/test_codex_model_preflight.py::test_model_missing_from_the_catalog_blocks_before_any_prompt` | catalog に無い model（gpt-5.5 型）が、実行して 400 を受けるまで分からない退行 |
| `::test_a_catalogued_model_is_never_reported_as_verified_without_an_allowlist` | gpt-6-sol 型（catalog にあるのに 400）を `OK` と主張する退行。`OK` は許可リストにもあるときだけ |
| `::test_a_model_outside_the_allowlist_blocks` | 許可リスト（実測した集合）外の model が日次を通る |
| `::test_a_model_in_both_allowlist_and_catalog_is_ok` / `::test_allowlist_member_missing_from_catalog_still_blocks` | 許可リストだけを信じて catalog との食い違いを見逃す |
| `::test_unpinned_model_from_the_shared_config_is_a_warning_until_pinning_is_required` | 共有 config.toml 由来の model が、固定必須の運用でも通る |
| `::test_unreadable_or_stale_catalog_never_blocks_and_never_verifies` | 読めない・古い catalog を根拠に日次を止める（誤停止）/ 確認できたことにする |
| `::test_read_models_cache_parses_slugs_and_fetch_time` | codex の `fetched_at`（9 桁小数秒・Z）を読めず catalog が常に「古い」扱いになる |
| `::test_run_preflight_reads_files_only` | 設定ファイル以外（prompt・ネットワーク）に出ない |
| `tests/contract/test_codex_env_single_source.py` | 3 つの Codex worker の model 設定が別々に書かれて食い違う（AGENTS §8） |

## 日次の入口ゲート・保留（INV-76 / INV-78）

`tests/unit/test_daily_credit_hold_workflows.py`。time-skipping の Temporal 環境で、ゲートの Activity
は本物（`PipelineActivities.daily_gate` / `production_gate`）、breaker・保留は fake の port、工程の子
workflow は fake。有料 provider は呼ばない。

| テスト | 止めるもの |
|---|---|
| `test_open_breaker_keeps_planning_and_script_but_starts_no_paid_work` | breaker が開いていても production へ進んで同じ有料呼び出しを繰り返す / 逆に台本まで止めてしまう。保留が `COMPLETED`（成功）で記録される |
| `test_an_unconfirmed_refusal_is_not_called_waiting_for_credits` | 原因未確定の 403 を「残高不足」と断定して保留理由に書く |
| `test_half_open_breaker_is_not_used_by_the_daily_run` | 所有者承認の上限付き再開の枠を日次が先に使う |
| `test_closed_breaker_runs_the_whole_pipeline_as_before` | ゲートの導入で通常運転が変わる（退行） |
| `test_a_pipeline_not_started_by_daily_is_never_stopped_by_the_gate` | API / 統一再開の pipeline を日次のゲートが止める（所有者承認の再開が拒否される） |
| `test_backlog_at_the_cap_creates_no_episode_and_calls_no_codex` | 保留が上限でも Episode・企画・Codex を増やす |
| `test_seven_days_of_open_breaker_are_bounded_and_repeat_no_paid_attempt` | 7 日開いたままで Episode が増え続ける・有料呼び出しが毎日再試行される・過去の Episode が再開される |
| `test_unreconciled_reservations_are_reported_but_do_not_stop_a_new_episode` | 入力単位の INV-67 を越えて、未照合があるだけで日次が止まる |
| `test_a_blocking_codex_preflight_fails_the_daily_run_before_any_codex_call` | 必ず失敗する model で Planner / Codex を呼ぶ・原因が失敗に残らない |
| `test_an_unverified_codex_model_does_not_stop_the_run` | 「使える証明が無い」だけで日次を止める誤停止 |
| `test_unwired_ports_behave_as_before_and_say_so` / `test_open_breaker_without_a_hold_writer_...` | 結線前の挙動（従来どおり）と、結線ミスで有料の生成へ進む事故 |
| `test_histories_recorded_before_adr_0058_replay_without_nondeterminism` | 旧履歴が新コードで replay できない（実行中・保持期間内の workflow を壊す）。fixture は 40cf2ca の workflow を fake と走らせた実履歴 |

既存の `tests/unit/test_pipeline_workflows.py`・`test_log_workflow_replay.py`・`tests/integration/test_pipeline_schedule.py` には、新しい必須 Activity（ゲート 2 つ・Codex 事前診断）の
fake を fixture に**足しただけ**（アサーションは変えていない）。足さないと workflow が未登録 Activity で
失敗するため。

## watchdog の分類（INV-77）

`tests/unit/test_hold_watchdog_classification.py`。

| テスト | 止めるもの |
|---|---|
| `test_a_recent_hold_is_not_reported_as_an_overdue_episode` | 意図した停止（保留）を完成期限超過として通報する。同時に `held_*` に出ず見えなくなる |
| `test_without_the_hold_the_same_episode_is_an_overdue_failure` | 上の除外が効きすぎて、保留でない Episode まで黙る（対照） |
| `test_a_blocked_hold_is_not_a_stall_until_it_is_old` | `blocked` の保留が猶予 0 の停滞検査で即通報される |
| `test_a_long_hold_escalates_through_the_existing_stalled_kind` | 保留が長引いても誰にも通報されない / 新しい kind（CHECK 拡張）が要る / 行・通知が毎時増える / 解放後も開いたまま |
| `test_a_real_failure_is_still_reported_next_to_a_hold` | 保留があるだけで他の本物の失敗が埋もれる |
| `test_an_unreadable_hold_table_does_not_hide_anything` | 保留を読めないことを健全として扱う |
| `test_a_day_skipped_for_backlog_is_not_a_daily_failure` | 意図して Episode を作らなかった日が `DAILY_WORKFLOW_FAILED` になる |

## watchdog の保留読み取り失敗（`test_hold_watchdog_classification.py`, review-E P2-10）

- `test_an_unreadable_hold_table_is_visible_in_the_result_and_logged_as_error`: 読み取り失敗は従来 WARNING だけで従来の停滞検査に倒れ、失敗自体が誰にも見えなかった。ERROR ログ `HOLD_READER_FAILED` と `WatchdogResult.hold_reader_failed` で見せる（CHECK は広げない）。成功時は false のまま（反対側）。
