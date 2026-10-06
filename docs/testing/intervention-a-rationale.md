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
