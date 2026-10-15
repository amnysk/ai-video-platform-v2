# JP-MAS-002 P2-A 既知3問題の解消: テスト設計の理由

ADR-0044 §3 の3問題（台本の作り直し / 再開設定の一本化 / flag OFF の再開）を解消した変更のテスト。
既存テストの変更は無い（追加のみ）。

## 1. 台本の作り直し（tests/unit/test_script_regeneration_after_brief_change.py）

`ScriptWorkflow` が呼ぶ Activity（`mark_episode_in_progress` -> `create_script_job` -> `generate_script` ->
`mark_script_ready`）を順に呼ぶ。以前の `test_a_script_made_under_an_older_creative_brief_is_not_adopted` は
再生成のために job を手で作っており、`create_script_job` の返す成功済み job を `generate_script` が
`start` して `InvalidTransitionError`（`succeeded + started`）になる本番の経路を踏んでいなかった。
失敗点は2つ: (1) その `JobRepository.start`、(2) Episode が `storyboard_ready` 等の駐機点にいると
`mark_episode_in_progress` の `workflow_started` が拒否される。

- `test_a_changed_brief_after_a_successful_script_gets_a_new_job_and_script`: brief 変更後、新しい job で
  再生成され、旧 job も残る（2本）。
- `test_old_script_is_kept_as_history_and_new_one_is_current`: 旧台本 Artifact は消えず version 1 のまま
  一覧され本体も読める。現行は新版。
- `test_an_activity_retry_returns_the_new_queued_job`: 作り直し後の `create_script_job` の再試行は
  同じ新 job を返す（job が増え続けない）。
- `test_an_unchanged_brief_reuses_the_script_without_a_new_job`: brief 不変なら job も増えず生成器も呼ばない。
- `test_reservation_follows_the_brief`: 新 brief は新しい予約、同じ brief の再実行は同じ予約（二重課金なし）。
- `test_the_same_job_is_reused_when_the_latest_one_is_still_open`: 非終端の job は新しく作らない。
- `test_a_legacy_episode_keeps_returning_the_first_job`（INV-55）: brief の無い Episode は `existing[0]`。
- `test_script_stage_reruns_when_the_episode_has_moved_past_script_ready`: 駐機点にいる brief 付き Episode は
  `STAGE_ADMITTED`（既存の辺）で入り直して台本を作り直せる。
- `test_a_legacy_episode_past_script_ready_is_still_rejected`: 上の入り直しは brief 付きだけ。遷移表を緩めない。

テストの生成器は呼ぶたびに別の台本を返す。同じ内容は同じ Artifact 行に畳まれる（`record` の規則、ADR-0012）ため、
そうしないと「新しい版」が別行にならず検査にならない。

## 2. 再開設定の一本化（tests/unit/test_pipeline_options_single_source.py）

`PipelineOptions` を設定から作る関数と agentic の起動仕様を作る関数をそれぞれ1つにした
（`infrastructure/temporal/pipeline_options.py`）。API の再開・介入 worker・CLI が同じ関数を使う。

- `test_daily_schedule_input_is_unchanged_by_the_extraction`: 日次 Schedule の入力 `options` が、抽出前に
  `daily_episode_input_from_settings` が直接組んでいた値（テスト内に写しを置く）と、既定の設定でも
  既定外の設定でも一致する（Schedule の入力が変わると既存 Schedule の更新が走る）。
- `test_the_task_queue_is_the_options_queue`: agentic の task queue は `options.pipeline_task_queue`。
  既定は従来の定数 `AGENTIC_EPISODE_WORKFLOW[1]` と同じ（既定の環境で何も変わらない）。
- `test_api_cli_and_worker_paths_build_the_same_start`: 同じ Settings（並行数・round 数・render profile を
  既定外にしたもの）で、API の `start_agentic_workflow`・CLI の `TemporalAgenticStarter.start`・介入 worker の
  `_choose_pipeline` が `(workflow 名, 入力 dict, task queue)` を完全に一致させる。負債は「API は既定の
  PipelineOptions、worker は設定」の食い違いだった。
- `test_a_custom_queue_reaches_every_path`: queue を既定から変えた options が API と worker の両方で
  同じ queue に届く（Settings には queue の項目が無いので、options の差し替えで検査する）。

## 3. flag OFF の再開と worker の確認

tests/unit/test_resume_feature_disabled.py:

- `test_api_resume_of_a_pinned_episode_with_the_flag_off_is_409_and_starts_nothing`: 固定計画を持つ Episode を
  flag OFF の API で再開すると 409 `{"error": "feature_disabled", ...}`。agentic も legacy の pipeline も
  start しない（拾う worker の無い workflow を作って「成功」と返さない）。
- `test_api_resume_of_a_legacy_episode_is_unaffected_by_the_flag`（INV-55）: legacy は flag に関係なく従来どおり。
- `test_worker_resume_of_a_pinned_episode_with_the_flag_off_starts_nothing`: 実 Temporal（time-skipping）と本物の
  Activity で、flag OFF の介入 worker は何も start せず、`pipeline.outcome=not_needed` / `reason=feature_disabled`
  を command の結果に残す。workflow のコードは変えていない（check Activity の応答だけで止める）ので replay 互換。
- `test_worker_resume_of_a_legacy_episode_with_the_flag_off_still_starts_the_pipeline`: legacy は影響を受けない。

tests/unit/test_resume_api_agentic.py（既存テストの変更）: fixture に `get_settings` の上書き
（`jpmas2_agentic_workflows=True`）を足した。ADR-0044 で「flag OFF の再開は拒否」が契約になったため、
これらの既存テスト（ON の環境での agentic 再開）は ON を明示する必要がある。アサーションは変えていない。

tests/unit/test_agentic_starter_worker_check.py（`TemporalAgenticStarter.probe_workers`、fake client）:

- `test_both_queues_with_pollers_is_pollers_present`: 設定の pipeline queue（workflow）と agents queue
  （activity）の両方を問い合わせ、両方に poller があれば `pollers_present`。
- `test_a_queue_with_no_pollers_is_no_pollers`: どちらかに poller が無いと確認できれば（他方が失敗していても）
  `no_pollers`。
- `test_any_error_without_a_definite_answer_is_unknown`: 確認できなければ `unknown`（「動いている」と言わない）。
- `test_a_client_without_a_service_is_unknown_never_raises`: client が問い合わせられなくても例外を投げない。
- `test_start_reports_the_worker_check_and_survives_a_failing_probe` /
  `test_an_already_started_workflow_still_reports_the_check`: `start` の結果に `worker_check` が載り、probe の失敗は
  起動の成否を変えない。既に走っている場合も確認結果を返す。
