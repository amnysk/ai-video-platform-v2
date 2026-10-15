# 介入の適用（JP-MAS-001 担当 B）のテストの理由

ADR-0041、契約は `docs/domain/intervention.md` §2–§9。担当 B が持つ不変条件は INV-41〜44・INV-46。
単体テストは SQLite（`tests/conftest.py`）と Temporal の time-skipping 環境で、外部（LLM・fal・YouTube）は
呼ばない。PostgreSQL の行 lock でしか起きない競合（hold と dispatch）は
隔離環境の integration（担当 B2 / D。まだ書いていない）で検査する。

## dispatch gate（`tests/unit/test_intervention_gate.py`）

守る契約: 有料 dispatch・投稿の開始許可は、Episode の制御状態と必須記録を**同じ transaction**で照合する
（INV-44 / INV-46）。前身 repo の「停止したつもりで課金が続いた」型の事故を、台帳の境界1か所で止める。

- `test_an_episode_without_a_control_row_dispatches_and_nothing_is_written`: 日次・旧 Episode は行を作らず通る。
  gate が行を作ると全 Episode に制御行が増え、日次経路の挙動（とデータ）が変わる。
- `test_a_non_running_episode_refuses_dispatch_and_leaves_the_reservation_unsent`（4状態）: running 以外は拒否し、
  予約は `reserved` かつ `dispatched_at IS NULL`（送っていない証拠）。ここが崩れると resume 時に二重課金か
  人手照合待ちになる。
- `test_upload_dispatch_is_refused_while_held_but_an_in_flight_session_is_not_reopened`: 投稿は新しい session の
  開始だけを拒否し、dispatch 済み session の再入（in-flight）は拒否しない。再入まで拒否すると受理位置が分からなく
  なる経路を増やす。
- `test_new_mode_requires_a_record_for_the_current_script_and_storyboard` /
  `test_legacy_mode_with_a_row_does_not_require_records`: INV-46。記録の種類（agent_run・decision_record）の
  どちらでも足り、legacy は要求しないこと。
- `test_a_reservation_refused_by_the_gate_is_reused_after_resume`: 実際の `PaidJobRunner` で、拒否された予約が
  resume 後に**同じ予約 id**で dispatch され、予約が1行のままであること（provider の submit は1回）。gate を
  単体で見るだけでは、既存の冪等経路（`_plan_round` の「reserved + 未 dispatch」分岐）と噛み合うかが分からない。

## command の受付と適用（`tests/unit/test_intervention_commands.py`）

守る契約: command 行は transactional outbox で、受付は1 transaction、適用は制御行の lock の下で冪等
（INV-42/43/44、`docs/domain/intervention.md` §4）。Temporal を使わず `apply_command`（Activity の本体）を
直接呼ぶ。配送の重複・Worker 障害は「同じ関数を二度呼ぶ」に還元できるので、この水準で検査できる。

- `test_hold_is_requested_in_the_receiving_transaction_and_not_reported_as_done`: hold は受付の commit で
  `hold_requested`（dispatch gate が効く）だが、command は `received` のまま（完了と表示しない）。
- `test_same_id_and_payload_returns_the_existing_command` / `test_same_id_with_another_payload_is_refused`:
  クライアントの再送は行もイベントも増やさず、id の使い回しは 409 になる。
- `test_receive_refuses_missing_scope_ai_set_mode_bad_payload_and_unknown_episode`: 受付で拒否するものは
  行を作らない（拒否が outbox に残って再配送されない）。`set_mode` は scope を持っていても AI には許さない。
- `test_comment_is_applied_at_receipt_and_reapply_returns_the_stored_result` /
  `test_applying_the_same_command_twice_is_one_logical_change`: 終端済みの command の再適用は保存済み result を
  返すだけで、制御版・時刻・イベント数が変わらない（apply の commit 後に Worker が落ちた場合の復旧）。
- `test_user_constraint_is_recorded_with_verifiable_target_ids`: 制約は自由記述でなく検証できる対象 id を持つ。
- `test_hold_becomes_held_and_lists_in_flight_unknown_and_undispatched`: HELD の応答が予約台帳の3分類を
  正しく分け（spent は載せない）、provider の job 参照を出さない。
- `test_a_stale_expected_control_revision_is_a_conflict_and_changes_nothing`: INV-43。resume も hold も
  期待版が古ければ conflict で、状態を変えない。
- `test_resume_runs_again_from_the_next_stage`: resume の開始工程と pipeline の start 入力、follow-up の記録後は
  再配送が pipeline を二度 start しない。
- `test_resume_is_refused_while_an_unknown_outcome_reservation_exists`: 成否不明は人の照合が先（INV-15）。
- `test_release_checkpoint_binds_the_approval_to_the_active_manifest`: 古い manifest への承認は conflict、
  現在の manifest への承認はその manifest に結び付く。
- `test_set_mode_by_a_human_uses_the_default_co_production_points` /
  `test_cancel_is_recorded_at_receipt_and_cannot_be_resumed`: mode の既定地点、cancel は記録だけで resume 不可。

## ChangeSet（`tests/unit/test_intervention_changes.py`）

守る契約: INV-41（新 revision・旧版と注記が残る）、INV-43（古い版の上書きをしない）、予算と AI の権限
（§8・§9）、そして**課金の安全**（修正で無関係な有料素材を作り直さない／旧版の結果を新しい採用に混ぜない）。
store・manifest・台帳は実物（SQLite）、成果物は InMemoryArtifactStore、有料 provider は呼ばない。

- `test_script_edit_creates_a_new_revision_and_keeps_the_old_one_and_comments`: 新しい世代・旧行と旧オブジェクトが
  残る・`parent` と `rebound_to`・新 manifest（親と原因 command）・判断記録の actor と差分・コメントの anchor が
  旧版を指したまま・古い manifest の approval だけが `review.invalidated`・INV-46 の必須記録を満たす、を1本で
  見る。どれか1つでも欠けると「誰が何を変えたか」を後から辿れない。
- `test_script_edit_does_not_touch_image_or_video_identity`: 台本の修正で画像・動画の現行行と入力指紋
  （有料の再生成の判定キー）、storyboard が変わらず、有料予約も作られない。音声・manifest だけ降りる。
- `test_a_stale_base_revision_is_a_conflict_and_marks_the_change_set` /
  `test_an_expected_target_revision_mismatch_is_a_conflict` /
  `test_proposals_against_a_stale_base_are_conflicts_and_do_not_create_change_sets`: 人と AI が同じ版を
  変えたとき後着が上書きしない（設計 §17-4）。
- `test_ai_may_apply_only_zero_cost_changes_that_respect_user_constraints`: 制約に触れる AI の案は保存されるが
  AI は適用できない／有料の変更は AI に適用させない／判断記録の actor は AI のまま。
- `test_reject_change_closes_a_proposal_once` / `test_changes_are_refused_while_a_stage_runs_or_after_upload`:
  走っている工程が旧版で下流を作り直す・投稿済みを再投稿する経路を作らない。
- `test_a_paid_change_without_a_rate_is_unverifiable_and_with_a_rate_is_checked`: 換算未設定は
  `budget_unverifiable`（推測で通さない）、上限超過は `budget_exceeded`。見積りはそのシーンの前回の予約に
  adapter が書いた値（fal adapter を import しない / INV-18）。
- `test_a_paid_change_without_a_ledger_estimate_is_unverifiable`: 見積りの材料が無いとき 0 円として通さない。
- `test_scene_override_reuses_the_override_artifact_and_regenerates_only_that_scene`: 差し替えは ADR-0035 の
  `scene_visual_override` として保存され、画像・動画の Activity と同じ読み口（`load_current_override`）が読む。
  他シーンの現行は変わらない（INV-33）。
- `test_a_planner_override_without_change_set_keeps_its_json_shape`: 契約に足した `change_set_id` が既存の
  代替映像案の正準 JSON（sha256）を変えない。
- `test_an_old_version_result_cannot_become_the_adopted_member_after_a_change`: 送信済み job のあるシーンの
  差し替えは拒否し、結果が旧版の入力指紋で保存された後なら適用でき、旧版は行として残るが新しい manifest に入らず、
  旧指紋での再利用も起きない。差し替えの拒否を外すとこのテストが落ちることを手で確認した。

## Temporal 接続（`tests/unit/test_intervention_workflows.py`）

守る契約: command の配送が重複・クラッシュしても適用は1回（INV-42）、pipeline の起動は1つ（INV-30）、
旧い履歴・日次実行は新方式に触れない（`workflow.patched`）、待機は黙って進まない（INV-45）。
time-skipping 環境と SQLite、工程の子 workflow は同名の fake、Activity は本物。

- `test_resume_applies_once_starts_the_pipeline_once_and_reports_duplicates`: 同じ command の再送は保存済み
  result を返し pipeline を増やさない／走っている間の別 resume は `already_running` と記録される。走り続ける
  fake pipeline が待機中に timeout しないよう、この fixture は時間の自動スキップを止める
  （スキップすると実行中の pipeline が timeout し、重複起動の検査が成り立たない。テスト側の不備だった）。
- `test_a_crash_after_the_db_apply_recovers_to_the_same_single_change`: apply の commit 後に落ちた command を
  再配送すると、再適用せず（制御版・event が増えない）pipeline だけ始まり、`replayed=true` が返る
  （follow-up の追記が `replayed` を上書きしていたのは実装の不備）。
- `test_redelivery_starts_undelivered_commands_and_marks_them_dispatched`: API の start 失敗で取り残された
  command を Schedule 側の workflow が拾い、配送済みを記録する。
- `test_a_control_gate_run_parks_at_a_review_point_and_resumes_after_release`: 共同制作では工程の前で
  `parked`、`checkpoint.waiting` は1回、現行 manifest への解放で次の工程から再開する。履歴は replay できる。
- `test_a_held_episode_parks_before_the_first_stage`: 停止中は最初の工程も起動しない。
- `test_the_default_run_records_no_patch_marker_and_no_control_gate_activity` /
  `test_the_control_gate_run_records_the_patch_marker`: 既定の実行は patch の marker も gate の Activity も
  履歴に足さない／gate の実行だけが `jpmas-control-gate-v1` を記録する。marker は `core_patch` の
  details にあるので id を読んで比べる（文字列検索では base64 に隠れて見つからない）。工程の fake は
  render / upload の task queue も指す（指さないと pipeline が完了せずテストが止まった）。
- `test_a_retried_control_gate_activity_emits_checkpoint_waiting_once`: Activity の retry・再実行で
  `checkpoint.waiting` が重複しない（INV-40 / INV-17）。

## 注記の解決（`tests/unit/test_intervention_resolve.py`、`tests/contract/test_migration_0017_resolve_annotation.py`）

守る契約: INV-41（元の注記を消さず追記）、INV-42（再送で1行1 event）、AI に追加権限を与えない。
`review.resolved` は定義だけで発生源が無かった（ReviewPacket の「未解決コメント」が減らせなかった）ので、
`resolve_annotation` を足した。

- `test_the_contract_declares_the_command_scope_and_a_strict_payload`: scope は `comment`、payload は
  余分な項目・未知の `resolution`・長すぎる `note` を拒否する（単一の宣言元は contracts）。
- `test_resolving_appends_a_resolution_row_and_one_event_and_keeps_the_original`: 元の行が無変更、
  `resolution` 注記が対象を指す、event は1つ、再適用・再送でも行と event が増えない。
- `test_a_second_resolution_of_the_same_annotation_is_rejected_invalid_state`: 二重解決は `invalid_state`
  （既存の語彙。新しい理由は足さない）で event を増やさない。
- `test_only_comments_and_findings_of_this_episode_can_be_resolved`: 他 Episode・存在しない id は
  `not_found`、制約など別種の注記は `invalid_payload`。他人の Episode の注記を操作できない。
- `test_ai_may_resolve_with_comment_scope_only`: `comment` が無ければ forbidden、有れば適用され actor は AI のまま。
- `test_a_resolved_comment_leaves_the_read_model_and_review_packet_open_lists`: 既存の読み口
  （read model の `status=resolved`、reviewer の `open_comments`）まで通しで反映される。
- 0017: DB の CHECK は 0016 の literal 語彙なので、契約に型を足しても migration が無いと実 DB で INSERT が
  落ちる。0017 の語彙が契約と一致すること、0016→0017→0016 で CHECK が増減することを検査する。
  0016 の語彙テストは「0016 の凍結値 = 契約 − 後続 migration が足した型」に変えた（仕様変更が
  ADR-0041 addendum として先に承認済みのため）。

## 配備（M1）
- `tests/contract/test_compose_workers.py`（`intervention-worker` を表に追加）: 他の worker と同じ形（image・
  command・healthcheck の queue・依存・capability）で compose に在ること。worker が compose に無いと
  command は永久に配送されない。`test_intervention_worker_budget_env_follows_the_contract_defaults`:
  compose の予算の既定が contracts と食い違わず、換算レートは既定なし（宣言元は1つ / INV-47）。
- `test_updating_the_intervention_redelivery_schedule_keeps_its_pause`: 再登録で運用者の pause を外さない。
- `tests/unit/test_intervention_run_worker.py`: レート未設定の起動時警告は1回、設定済みなら出ない。
  黙って「有料の変更が全部通らない」状態にしない。

## 取り残しの回復（M2 / M3、`tests/unit/test_intervention_workflows.py`）
- `test_a_command_killed_between_apply_and_follow_up_is_started_once_by_redelivery`: 適用の commit 後・
  follow-up 前に落ちた resume は、再配送が pipeline を**1回だけ**始め、記録後は再び拾われない。
  以前は follow-up が無いと永遠に始まらなかった（再開が失われる）。
- `test_a_start_that_became_stale_after_apply_starts_nothing_and_says_why`: 適用の後に hold が入った
  （制御版が進んだ）古い resume は始めない（`not_needed` / `control_revision_changed`）。
- `test_a_finished_episode_is_not_restarted_by_a_redelivered_resume`: 制御版が同じでも投稿済み・終端の
  Episode は始めない（`episode_closed`）。再配送が終わった pipeline を再起動する経路を塞ぐ。
- 配送済みの記録は start 成功後の別 Activity にした（列挙しただけで dispatched にすると、start に失敗した
  command が二度と拾われない）。既存の再配送テストが配送済みの記録を引き続き検査する。

## AI の resume は有料の生成を始めない（`tests/unit/test_intervention_ai_resume.py`）
`apply_change` は AI に有料の変更を禁じるが、変更は出力を降ろすだけで、作り直す（課金する）のは次の resume。
scope `resume` を持つ AI が同じ課金を始められると、禁止が迂回される。
- 音声・manifest だけが欠けた AI の resume は適用される（無料の再開を妨げない）。
- 画像が欠けた AI の resume は `forbidden` + `detail` で拒否され、制御状態も pipeline も変わらない。
- 同じ状況の人の resume は適用される（人は影響を受けない）。
- storyboard が無い（storyboard からやり直す）AI の resume も拒否される（全シーンの画像を作るため）。
- `test_intervention_changes.py` は AI の有料 apply が `ai_paid_apply` を返すことも検査する（原因の区別）。
