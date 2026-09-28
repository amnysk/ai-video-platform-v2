# ADR-0035 のテスト設計根拠

事故: 2026-09-26/27 の fal 422 `content_policy_violation`（`body.image_url`、実在人物の肖像）で
日次 Episode が止まり、ADR-0034 の緩和策（prompt 版を上げる）は全シーンを再課金することが
連続試験で分かった。以下は各テストが何を守るか。

## 共有基盤（`tests/unit/test_scene_visual_foundation.py`、unit）

純粋な契約と関数なので unit で固定する。DB も provider も要らない。

| テスト | 守るもの / 落ちたら何が起きているか |
|---|---|
| `test_old_storyboard_scene_without_visual_subject_still_parses` | 本番に残る旧 storyboard が読めること。落ちれば停止中 Episode の再開が契約違反で止まる |
| `test_storyboard_scene_accepts_a_visual_subject` / `test_visual_subject_vocabulary_is_closed` | 映像対象は閉じた語彙。自由文字列を許すと「何を描くか」の判定・集計ができない |
| `test_rejected_input_vocabulary` | 拒否対象の語彙が DB の CHECK（migration 0014）と一致する前提 |
| `test_scene_visual_override_round_trips_through_parse_artifact` | 代替案が `parse_artifact` の唯一の入口で読めること（INV-10） |
| `test_scene_visual_override_rejects_incomplete_plans` | 根拠（`rationale`）・対処した拒否（`rejection_ids`）の無い代替案、revision 0 を保存しない。人間が後で判断できる記録であることを契約で強制する |
| `test_apply_override_replaces_only_visual_fields` | 代替案は映像の項目だけを替え、尺・台本対応を替えない。落ちれば音声・字幕・尺まで動き、無関係な成果物が無効化される |
| `test_apply_override_without_override_is_identity` / `..._for_another_scene_is_refused` | 代替案の無いシーンは不変、別シーンの案は適用できない |
| `test_fingerprint_ignores_position_on_the_timeline` | 前のシーンの尺が変わっただけで後ろのシーンが作り直しにならない |
| `test_fingerprint_changes_when_what_the_scene_shows_changes` | 映像の内容が変われば必ず新しい指紋（古い画像の誤再利用を防ぐ。ADR-0017 の「文面を変えたら版を上げる」の代わり） |
| `test_fingerprint_is_per_scene` | **事故の核の裏返し**。別シーンの内容は指紋に入らない。落ちれば1シーンの差し替えが全シーンを再課金する |
| `test_fingerprint_of_a_scene_without_subject_is_stable_across_the_new_optional_field` | 任意項目の追加で旧シーンの指紋が変わらない（旧 Episode の再利用互換） |
| `test_recovery_limits_are_positive_and_scene_limit_fits_episode_limit` | 上限の定義元が1箇所にあり、矛盾しない（INV-34 の前提） |

## migration 0014（`tests/contract/test_migration_frozen_vocabulary.py`、contract）

| テスト | 守るもの |
|---|---|
| `test_0014_downgrade_vocabulary_is_frozen_at_8a8c499` | downgrade 先の語彙が履歴どおり（enum が将来変わっても変わらない） |
| `test_0014_upgrade_adds_exactly_the_adr_0035_values` | 追加は3語だけ。今の enum と DB の CHECK が一致 |
| `test_0014_scene_scope_matches_the_models` | 新しい3語はシーン単位（scene_id 必須） |
| `test_0014_downgrade_restores_every_upgraded_check` | 張り替えた CHECK は downgrade で全部戻る |
| `test_0014_legacy_rejection_classification_reads_both_summary_formats` | 本番に実在する2行（`dedcf315` sb4 / `ec44fadd` sb2、pydantic repr 形式）を `image` と判定する。一度きりの補完が誤判定すると、停止中 Episode の拒否画像が再送される |

既存テスト2件（`test_0004_scene_scope_types_are_frozen`、`test_0006_upgrade_adds_exactly_the_phase6_values`）は、
今の enum ではなく 0014 が凍結した「前」の値と比べるよう変えた。これはファイル内の既存の前例
（0005・0006 追加時の同じ書き換え）どおりで、仕様変更は ADR-0035 で承認している。

## 拒否の構造化と再送禁止（INV-32）

| テスト | 層 | 守るもの / 落ちたら何が起きているか |
|---|---|---|
| `test_fal_queue.py::test_content_policy_rejection_is_structured_from_the_body` | unit | fal の 422 body（実際の形）から対象・理由・種別を例外に載せる。落ちれば拒否が文字列にしか残らず、画像かテキストかを機械で区別できない |
| `test_fal_queue.py::test_submit_rejection_on_the_prompt_is_structured` | unit | submit 時の拒否も同じ構造。テキスト起因は `prompt` |
| `test_fal_seedance_video.py::test_image_url_rejection_reaches_job_failed_structured` | unit | adapter の poll → `JobFailed` まで構造が落ちずに届く（ここで落とすと台帳に残らない） |
| `test_paid_job.py::test_content_rejection_is_recorded_structured_with_the_input_image` | unit（sqlite） | spent と同じトランザクションで `provider_rejections` に1行、入力画像 sha256 つき。後段の復旧・測定の唯一の材料 |
| `test_paid_job.py::test_rejected_image_is_not_resubmitted_with_different_text` | unit（sqlite） | **INV-32 の核**。テキストを変えた（別 input_hash）だけの同じ画像は予約も submit も作らない。落ちれば「言い換えて同じ画像を再送」→ 同じ拒否 → 課金、が起きる |
| `test_paid_job.py::test_a_different_image_is_not_blocked` | unit（sqlite） | ゲートは画像単位。作り直した画像は通る（過剰に止めて復旧できない、を防ぐ） |
| `test_paid_job.py::test_a_prompt_rejection_does_not_block_the_image` | unit（sqlite） | 拒否位置がテキストなら画像は止めない |
| `test_paid_job.py::test_unstructured_rejection_is_still_recorded_as_unknown` | unit（sqlite） | 構造の無い拒否も件数に入る（拒否率の分子から漏らさない） |

## 代替案の規則と上限（INV-34）

| テスト | 層 | 守るもの |
|---|---|---|
| `test_scene_alternative_rules.py::test_content_policy_rejection_rules_out_person_subjects` / `::test_other_rejections_do_not_rule_out_people` | unit | 人物を主題にしない制約は内容方針の拒否があるときだけ（一律禁止にしない） |
| `test_scene_alternative_rules.py::test_parse_*` / `::test_malformed_planner_output_is_not_repaired` | unit | LLM 出力は修復せず、形式不正は止める |
| `test_scene_alternative_rules.py::test_repeating_the_rejected_description_is_refused` / `::test_repeating_a_previous_alternative_is_refused` | unit | 同じ文面の作り直し（＝同じ判定の繰り返し）を採らない。大小文字・空白の違いでは別案にならない |
| `test_scene_alternative_rules.py::test_an_alternative_without_rationale_is_refused` | unit | 史実を損なわない根拠の無い案は保存しない（人間が後で読める記録） |
| `test_scene_alternative_rules.py::test_recovery_cost_counts_rejected_spends_and_rebuilds_only` | unit | 費用の定義: 拒否された試行 + 代替案後の作り直しだけ。通常の成功や無関係なシーンは数えない |
| `test_scene_alternative_rules.py::test_limits_*` | unit | 回数・費用の上限の境界 |
| `test_scene_alternative_activity.py::test_plans_and_saves_an_alternative_for_the_rejected_scene_only` | unit（sqlite + in-memory store） | 拒否シーンだけに案が保存され、planner 呼び出しが台帳に evidence 付きで残る |
| `test_scene_alternative_activity.py::test_activity_retry_returns_the_saved_plan_without_calling_the_planner_again` | 同上 | Activity の再実行で二重に計画しない |
| `test_scene_alternative_activity.py::test_blocked_again_on_the_same_plan_does_not_loop` | 同上 | 新しい拒否が無いのに再び止まった（案が画像に反映されていない等）なら計画しない。無限ループの防止 |
| `test_scene_alternative_activity.py::test_a_new_rejection_of_the_alternative_gets_a_second_plan` | 同上 | 代替案も拒否されたら、それを「試した案」として2回目を計画 |
| `test_scene_alternative_activity.py::test_scene_limit_stops_automation` / `::test_cost_cap_stops_automation_before_calling_the_planner` | 同上 | 上限は DB から数える（resume でリセットしない）。費用上限は planner を呼ぶ前に止める |
| `test_scene_alternative_activity.py::test_infeasible_plan_stops_with_the_planners_reason` / `::test_a_person_subject_after_a_likeness_rejection_is_not_saved` / `::test_no_planner_configured_is_needs_input` / `::test_scene_without_a_recorded_rejection_is_not_planned` | 同上 | 止まる条件はすべて needs_input で、何も保存しない |

## workflow の復旧ループ（`test_production_scene_recovery_workflow.py`、time-skipping + mock Activity）

Temporal の決定論の中で「どのシーンの何を呼び直すか」を固定する。有料 submit の回数をシーンごとに数える。

| テスト | 守るもの |
|---|---|
| `test_only_the_rejected_scene_is_rebuilt_from_its_image` | **事故の核の裏返し**。sb2 だけ画像から作り直し、sb1/sb3 の submit は1回のまま。記録される失敗も無い |
| `test_retry_blocked_on_resume_also_plans_an_alternative` | 旧 Episode の resume（再送禁止で止まる）も同じ復旧に入る |
| `test_planner_limit_stops_with_needs_input_and_the_reason` | 上限で止まったら理由つき needs_input、作り直しはしない |
| `test_workflow_never_asks_the_planner_more_than_the_scene_limit` | Activity 側の判定が壊れていても1実行の planner 呼び出しは上限回数まで |
| `test_other_failures_do_not_trigger_alternative_planning` | 403（資格情報）は内容の問題ではない。代替案を作らない |

止まる系の3件は1シーンで走らせる: 兄弟の cancel（`WAIT_CANCELLATION_COMPLETED`）は mock Activity が
受け取れず試験サーバが終わらないため（復旧の検査とは無関係の試験器の都合）。

## 測定（`test_production_metrics_script.py`、unit / sqlite）

拒否率・再試行・代替案・完成率・1本あたり費用の定義を固定する（本番では SELECT だけ）。


## 統合時に見つけた配線の穴（`tests/unit/test_production_video_activities.py`、unit）

| テスト | 守るもの |
|---|---|
| `test_await_time_content_rejection_records_the_input_image_and_blocks_it` | 本番の 422 は submit ではなく **await（result 取得）** で返る。各担当の単体テストは submit 側と paid_job 単体で画像キーを検査していたが、動画 Activity が `await_output` に入力画像の sha256 を渡していなかったため、本番の経路では拒否行の `source_media_sha256` が NULL になり、INV-32 の画像ゲートが一度も効かない状態だった。Activity を通して「await で拒否 → 画像キーが残る → 文面を変えた同じ画像の submit が予約前に止まる（provider への submit は1回のまま）」を固定する。Activity と PaidJobRunner の境界の配線なので、両方を本物で通す unit にした |
