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
