# JP-MAS-001 担当A（記録・読み取り）のテストを足した理由

設計 [JP-MAS-001 v2.0](../design/JP-MAS-001-v2.0.md)、契約 [intervention.md](../domain/intervention.md)、
[ADR-0041](../decisions/0041-decision-records-and-intervention.md)。全て SQLite + `InMemoryArtifactStore`
（unit）。実 LLM・MinIO・PostgreSQL は使わない（LLM は `FakeStoryGenerator`）。PostgreSQL 固有の
並行性はこの担当の範囲に無い。

## AgentRun / ContextSnapshot の捕捉（`tests/unit/test_intervention_capture.py`）

| テスト | 守る契約 | なぜこの層か | 捕まえる失敗 |
|---|---|---|---|
| `test_snapshot_is_written_under_the_run_prefix_and_the_row_points_at_it` | 設計 §6: snapshot は実際に渡した入力・model（設定値と明記）・prompt_version・request/response を持ち、正本は `agent-runs/{scope}/{agent_run_id}/`。行の sha256 が保存物と一致 | recorder 単体で保存物と行の対応を直接読める | 行だけ作って本体を書かない／別キーを指す／sha256 が保存物と食い違う |
| `test_token_usage_stays_null_unless_it_was_parsed` | intervention.md §6「取れなければ NULL。0 で埋めない」 | 列の既定値の問題なので最小の単位で | 0 埋め（未計測を「無料」と誤読させる） |
| `test_activity_retry_with_the_same_key_does_not_duplicate_the_run` | Activity の再実行で記録が増えない（id は冪等キーから決定的） | 同じ入力を2回流すだけで検査できる | retry ごとに agent_run が増え、判断の数が水増しされる |
| `test_secrets_are_redacted_before_the_snapshot_is_stored` | AGENTS.md §9 / INV-39 の趣旨: 秘密・signed URL を保存物に出さない | 保存物のバイト列を直接見る | Bearer・`sk-`・`X-Amz-Signature` が MinIO に平文で残る |
| `test_a_storage_failure_is_a_capture_error_and_writes_no_row` | 記録できなければ行を作らず `needs_input` で止める（自動の再ラウンドで有料呼び出しを増やさない） | 故障注入（put が例外）を最小で | 本体の無い行、または黙って続行して記録の無い成果物が下流へ流れる |
| `test_capture_needs_a_scope` | `agent_runs` は episode か topic_plan のどちらかに属する（CHECK と同じ規則をアプリ側でも先に） | 純粋な検証 | 行き先の無い snapshot |
| `test_exchange_recorder_is_transparent_outside_a_tape` / `..._keeps_the_request_of_a_failed_call` | prompt が生成器の内側で組まれる storyboard でも、実際に渡した prompt を捕捉できる。tape の外では挙動を変えない（旧経路） | decorator 単体 | 包んだだけで旧経路の結果が変わる／失敗した呼び出しの入力が消える |

## 判断記録・revision link（`tests/unit/test_intervention_records.py`）

| テスト | 守る契約 | なぜこの層か | 捕まえる失敗 |
|---|---|---|---|
| `test_choose_topic_uses_exactly_the_persisted_candidates` | 設計 §7「候補やスコアを後から架空に埋めない」。理由は reported、自己評価と規則の点数を混ぜない | 保存済みの plan から導く関数を直接 | 候補の追加・削除、自己評価を実測・規則として記録、却下理由の捏造 |
| `test_candidates_from_earlier_rounds_are_declared_missing` | 保存されない前 round の候補を「無い」と明記する（黙らない） | 同上 | 最終 round だけを全候補であるかのように見せる |
| `test_missing_reason_and_self_assessment_are_not_filled_in` | 古い候補 payload に理由・自己評価が無ければ none / null のまま | 同上 | 欠けた理由を補完する |
| `test_unknown_plan_derives_nothing` / `test_recording_the_topic_decision_is_idempotent` | 無いものは作らない・再記録で増えない | 同上 | 空の判断記録、重複行 |
| `test_evidence_check_is_against_the_snapshot_text` | 設計 §7: 根拠に挙げた資料が実入力にあるかを照合し、無ければ `unverified_ref` | 純粋関数 | 入力に無い資料を検証済みと記録 |
| `test_generation_decision_has_one_candidate_and_no_invented_rationale` | 1案生成は候補1件・理由 none | 純粋関数 | 生成物に理由・点数を後付け |
| `test_json_refs_are_found_at_any_depth` | 上流参照は成果物 JSON の `artifact_id`（入れ子を含む） | 純粋関数 | production manifest の scene 内参照を落とす |
| `test_derived_links_are_written_once_for_existing_upstreams` | 新方式の `derived_from` は実在する revision だけを冪等に書く | DB（FK）込みで | 存在しない revision への辺、重複辺 |
| `test_old_episodes_get_the_same_edges_by_reading_without_writing` | 古い Episode は読み取りで同じ辺を導出し、何も書かない | DB 込みで書き込みが無いことを数える | 読み取りが表を書き換える |
| `test_unreadable_artifacts_are_reported_not_skipped_silently` | 読めない成果物は「読めない」と返す | 空の store で | 辺が無いことと読めないことの混同 |

## 台本 Activity の記録（`tests/unit/test_intervention_script_capture.py`）

| テスト | 守る契約 | なぜこの層か | 捕まえる失敗 |
|---|---|---|---|
| `test_legacy_episodes_get_no_capture_and_no_records[None / legacy]` | 日次・古い Episode（行なし / mode=legacy）の挙動・履歴・出力を変えない（ADR-0041 Decision、設計 §2） | 実 Activity を Fake 生成器で通し、新しい表の行数と store の中身を数える | legacy でも snapshot・行・制御行を書く（日次の DB と MinIO を汚す） |
| `test_new_method_records_the_run_and_the_decision_with_the_artifact` | 新方式では実際の prompt・生出力・予約 ID を記録し、判断記録が成果物 revision と snapshot を指す | Activity 全体（予約→呼び出し→成果物→記録）の順序を通す | prompt の取り違え、結果 revision の欠落、tool 参照の欠落 |
| `test_capture_failure_fails_the_activity_and_records_no_artifact` | 記録が無ければ下流（有料生成）へ流さない。再開は LLM を呼び直さず、prompt は組み直して明記 | 故障注入（`agent-runs/` への put だけ失敗）＋同じ round の再開 | 記録失敗を握りつぶして成果物だけ残す／再開で LLM を二重に呼ぶ／組み直した prompt を captured と偽る |
| `test_reusing_an_existing_artifact_does_not_add_runs` | 同じ入力の再実行（再利用）は実行記録を増やさない | 同じ Activity を2回 | 再利用のたびに agent_run・判断記録が増える |

## storyboard Activity の記録（`tests/unit/test_intervention_storyboard_capture.py`）

| テスト | 守る契約 | なぜこの層か | 捕まえる失敗 |
|---|---|---|---|
| `test_legacy_storyboard_writes_nothing_new[None / legacy]` | 日次・古い Episode の storyboard は新しい表・保存物を何も作らない | 実 Activity | legacy の挙動変化 |
| `test_new_method_records_the_actual_prompt_and_links_the_script` | 生成器の内側で組まれた prompt を実物のまま記録し、入力台本 revision・予約 ID・`derived_from`（台本→storyboard）を残す | 実 Activity + prompt を内側で組む生成器（本物の adapter と同じ形） | 組み直し・推測の prompt を記録、上流 revision の欠落、辺の欠落 |
| `test_without_a_recorded_exchange_the_prompt_is_unavailable_not_invented` | 捕捉できなかった prompt は `unavailable`（補完しない） | tape を通さない生成器 | 空・推測の prompt を captured として記録 |
| `test_capture_failure_fails_the_storyboard_activity_without_an_artifact` | 記録できなければ storyboard を現行にしない（有料の production へ流れない） | 故障注入 | 記録の無い storyboard から有料素材が作られる |
| `test_the_storyboard_worker_routes_its_llm_through_the_exchange_recorder` | 本番の配線が LLM を包んでいる（包まなければ常に unavailable になる） | worker の配線は起動しないと通らないため source で検査 | 配線漏れで prompt が一度も捕捉されない |

## Topic Planner（producer）の記録（`tests/unit/test_intervention_topic_capture.py`）

| テスト | 守る契約 | なぜこの層か | 捕まえる失敗 |
|---|---|---|---|
| `test_capture_is_off_by_default` | 既定 OFF。日次の Planner は何も書かない（prompt の不変は既存の `test_planner_trend_opt_in.py::test_off_prompt_and_version_are_byte_identical_to_f209e7c` が守る） | 実 Activity 2つを通す | 設定を入れていないのに記録が増える |
| `test_the_worker_passes_a_capture_store_only_when_enabled` | 配線は設定だけで決まる | 配線関数を直接 | 設定と無関係に保存先が渡る |
| `test_enabled_capture_records_the_run_and_the_choice_with_the_plan` | 設計 §17-1「企画採用理由から実際の入力・成果物へたどれる」: plan → choose_topic → agent_run → 実際の prompt・生出力 | workflow を変えずに2つの Activity が同じキーを導くことを通しで | 一時置き場から写せない、判断記録が snapshot を指さない、候補が保存済みと食い違う |
| `test_a_staging_failure_fails_candidate_generation` | ON のとき記録できなければ失敗させる | 故障注入 | 記録が無いまま plan が確定する |
| `test_a_rerun_of_select_and_save_does_not_duplicate_records` | 保存後に応答を失った再実行で記録が増えない | 同じ Activity を2回 | 再実行で agent_run・判断記録が増える |

## 読み取りモデル・INV-45（`tests/unit/test_intervention_read_model.py`）

| テスト | 守る契約 | なぜこの層か | 捕まえる失敗 |
|---|---|---|---|
| `test_a_legacy_episode_reads_with_defaults_and_nothing_is_written` | 行の無い Episode は既定値（persisted=false）で読み、現行から manifest を組み、JSON 参照から依存を導き、何も書かない | SQLite で行数を数える | 読み取りが制御行を作る、古い Episode が読めない |
| `test_a_member_built_from_a_non_adopted_upstream_is_stale_unless_rebound` | 設計 §12: 採用していない上流から作った下流は stale。`rebound_to` は stale にしない（ADR-0041 §6） | 台本の差し替えを実データで | 旧台本の storyboard・音声を現行として示す／結び直しを無視 |
| `test_approval_is_current_only_for_the_active_manifest` | INV-45 の述語 | 純粋関数 | 旧 manifest・未結び付け・コメントを合格扱い |
| `test_an_approval_of_the_old_manifest_is_stale_after_the_manifest_changes` | INV-45 / 設計 §17-8「変更した manifest への古い合格判定を流用しない」 | `ensure_manifest` で manifest を実際に替える | 旧合格が新しい完成品の合格として残る |
| `test_a_comment_on_a_replaced_revision_stays_put_as_an_old_version_comment` | 設計 §8: アンカーを動かさず古い版のコメントとして示す。解決は別行 | 実データ | コメントの自動移動・消失、解決の取り違え |
| `test_received_commands_are_never_presented_as_done` | intervention.md §4: RECEIVED を修正完了として示さない。ChangeSet の影響・見積り・actor（AI 提案）を出す | 実データ | 受付を完了と誤表示、AI の提案を人の編集として表示 |
| `test_reservations_are_classified_and_llm_cost_is_unmeasured_not_zero` | 送信済み・成否不明・未送信の区別（intervention.md §5）、費用は台帳＋research の見積り、見積りの無い呼び出しは 0 にしない（§9） | 台帳を実際の遷移で作る | 成否不明を未送信と混同、LLM 費用を 0 と表示 |
| `test_records_events_and_derived_topic_decisions_are_listed` / `test_unknown_episode_is_none` | 制御・イベントの表示、存在しない Episode | 実データ | 制御状態の取り違え |
| `test_compare_scripts_per_segment` / `test_compare_storyboards_per_scene_and_others_generically` | 版の比較（台本は segment、storyboard は scene、他は JSON） | 実データ | 位置ずれの比較で無関係な差分を出す |

## ReviewPacket（`tests/unit/test_intervention_review_packet.py`、`tests/contract/test_review_packet_contract.py`）

| テスト | 守る契約 | なぜこの層か | 捕まえる失敗 |
|---|---|---|---|
| `test_no_secret_or_signed_url_reaches_the_packet_or_markdown` | AGENTS.md §9・設計 §8: 素材は参照だけ、秘密・signed URL を書き出さない | 素材本体・コメント・command・変更案に token らしき値を仕込み、JSON と Markdown の全文を検索 | 人・AI が入れた文字列経由の漏えい、signed URL の生成 |
| `test_unknowns_are_listed_instead_of_filled_in` | 欠測・未確認を `missing` に列挙（補完しない）、LLM 費用は unmeasured | 古い Episode の view から | 欠けた記録を黙って省く・0 と表示 |
| `test_rationale_is_kept_apart_from_tool_refs_and_results` | 理由（種別付き）と tool 参照・結果を別欄にする（設計 §7） | 判断記録を実際に書いて | 自己申告の理由と tool の結果の混同 |
| `test_markdown_shows_the_script_old_comments_and_unapplied_commands` | 人向けの出力に台本・古い版コメント・未適用 command・unmeasured が出る | Markdown を直接 | 受付を完了と誤表示、古いコメントの欠落 |
| `test_the_published_json_schema_matches_the_models` | 書き出した schema とモデルの一致（AGENTS.md §7/§8） | ファイル比較 | schema の更新漏れ |
| `test_each_proposal_is_exactly_a_propose_change_payload` 他 | 変更案ファイルの各案は `ProposeChangePayload` そのもの（担当C の取り込みと同じ検証）、基準 manifest の不一致・未知の欄を拒否 | 契約の検証 | 取り込みで拒否される形を書き出す、予算などの未知の欄を通す |

## 独立レビュアー（`tests/unit/test_intervention_reviewer.py`）

| テスト | 守る契約 | なぜこの層か | 捕まえる失敗 |
|---|---|---|---|
| `test_a_clean_manifest_passes_and_is_recorded_without_new_revisions` | RoleSpec REVIEWER: revision を作らない・LLM なし。所見は採用 manifest と agent_run に結び付き、判定は rule・自分の snapshot を指す | 実データで run_reviewer を通す | レビュアーが成果物を書き換える、所見がどの manifest のものか分からない |
| `test_narration_that_does_not_fit_its_scene_is_blocking` | 台本 Activity と同じ予算式で segment に所見を付ける | 実データ | 予算超過の見落とし、アンカーの欠落 |
| `test_a_storyboard_span_too_short_for_its_narration_is_blocking` | 既存の domain 規則（`check_storyboard_fits_narration`）を再利用 | 実データ | 区間不足の見落とし |
| `test_final_video_qa_and_its_production_manifest_are_checked` | 完成動画は採用中の production manifest から作られ QA 合格であること | 不一致→一致の2段 | 別の素材一式の完成動画を合格にする |
| `test_open_comments_stale_approvals_and_failed_verification_are_reported` | 未解決コメント・旧 manifest の承認（INV-45）・照合不合格を助言として出す。自分の所見をコメントとして数えない | 実データ | 旧承認の流用、照合結果の無視、所見の自己増殖 |
| `test_rerunning_on_the_same_inputs_does_not_add_records` | 同じ入力の再実行で記録が増えない | 2回実行して行数を比べる | 再実行のたびに所見・判定が重複する |

## Episode・Scene・Workflow への結び付き（migration 0018）

| テスト | 守る契約 | なぜこの層か | 捕まえる失敗 |
|---|---|---|---|
| `test_0018_adds_nullable_columns_and_indexes_and_downgrade_removes_them` | 0018 は `agent_runs` / `decision_records` に NULL 可の列と索引を足し、downgrade で戻る。既存行を強制しない | 実際に alembic を 0017→0018→0017 と動かして形状を見る（models 一致検査は別に走る） | NOT NULL で足して既存行が壊れる、索引の付け忘れ、downgrade で列が残る |
| `test_outside_an_activity_there_are_no_temporal_ids_and_nothing_is_invented` | Activity の外（test・CLI・API）で作る行と `agent.run.recorded` / `decision.recorded` は Temporal 識別子を持たない。`scene_id` も補完しない | `ActivityEnvironment` を使わない実 DB（SQLite）の記録 | 外側で偽の workflow_id を入れる、イベントに None の項目を足す |
| `test_inside_an_activity_the_run_the_decision_and_their_events_carry_the_ids` | Activity の内側では `activity.info()` から run・decision の行とイベント refs に workflow / run / activity を載せ、projection は**既存の**ログ項目（`workflow_id` / `run_id`）に出す | `temporalio.testing.ActivityEnvironment`（Temporal server 不要。Activity の入力の形は変えていないことも兼ねる） | 識別子の取り違え（run_id と workflow_run_id）、ログ契約に無い項目の発明 |
| `test_a_scene_override_decision_is_linked_to_its_scene_and_a_script_edit_is_not` | `scene_id` は payload が単一 Scene を明示する `scene.override_visual` の適用だけ。segment 単位の台本編集は NULL（推測しない） | 実際の propose→apply を通す | 台本編集の判断に Scene を当てずっぽうで付ける、override の Scene の欠落 |
| `test_search_filters_by_scene_and_summaries_show_the_links` | `search_decisions` / `GET /decisions` の `scene_id` 絞り込みと、要約に `scene_id` / `workflow_id` が出る | 実 API（ASGI）+ SQLite | 他 Scene の判断が混ざる、絞り込みの無視 |
| `test_explanation_shows_scene_and_workflow_links_or_lists_them_as_not_recorded` | explain は `scene_id` / `workflow_id` / `workflow_run_id` と agent_run の識別子を出し、無ければ null + `not_recorded`（補完しない） | 実 API。Activity の外／内（`ActivityEnvironment`）の両方 | 欠測を黙って省く、識別子の取り違え |
| `test_the_read_model_and_the_review_packet_list_the_links` | read_model の判断・実行記録と ReviewPacket の判断が同じ識別子を出す | 実データ（read model → packet 変換） | packet だけ識別子が落ちる |
| `test_search_decisions_can_filter_by_scene` / adapter の `scene_id` 行 | tool schema に `scene_id` があり、adapter は query にそのまま渡す | 契約／アダプタの表 | schema と route の食い違い |
