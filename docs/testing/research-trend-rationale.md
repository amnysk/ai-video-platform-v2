# テスト設計の根拠: Trend Research（ADR-0039、INV-36 / INV-37）

各テストが**なぜ必要なのか**を残す。B5 のテストが守るものは 6 つある。

1. 解釈器（LLM）の呼び出しも台帳の枠を通る。予算外の呼び出しをしない（INV-36。旧実装の穴）。
2. 観測（事実）と解釈（仮説）は別の欄で、解釈は実在する観測だけを根拠にする。
3. 欠損を 0 にしない・単一の総合スコアを持たない・差分と参考の平均を混同しない・動画の長さから Shorts と
   推定しない・視聴者を測定値として書かない。
4. 解釈器が無い・悪い提案・検索の失敗・枠切れのとき、Trend は `partial` / `blocked` で、`completed` に
   ならない（fail-closed）。
5. 読み口（`latest_trend`）は検証できた最新の `completed` の Trend か `None` だけを返す（B6 の前提）。
6. Tier A の Fake の拡張は既存の挙動を変えない。

実際に起きた事故から作ったテストではない。旧ブランチ（`claude/research` 8cede47、旧 ADR-0033）の意味を
ADR-0037 の分離した永続化の上で作り直したときに生じた境界から作った。DB は SQLite、ArtifactStore は
インメモリ、Provider・解釈器は Fake だけ（固定コーパス）。実ネットワーク・有料 API には出ない。

## `tests/contract/test_research_trend_contracts.py`（契約 = 最終防衛線）

| テスト | なぜ必要か / 落ちたら何が起きているか |
|---|---|
| `test_a_well_formed_trend_round_trips` | 生成側（build）と取り込み側（parse）が同じ契約を通る。片側だけ変わると保存物が読めない（読み口が常に `None` になる） |
| `test_an_interpretation_citing_an_unknown_observation_is_rejected` | 観測の無い解釈（LLM の作り話）が事実の顔で残る経路を契約で塞ぐ |
| `test_an_interpretation_citing_another_candidates_observation_is_rejected` | 別の動画の数字を根拠に別の題材を推すと、根拠の対応が嘘になる |
| `test_an_interpretation_is_only_a_hypothesis_with_a_basis` | 解釈を `fact` として置けない・根拠なしの解釈を置けない |
| `test_observations_carry_observed_at_and_a_method_named_by_the_metric` | いつの値か分からない数字・計算方法と名前の食い違う数字（差分を読み取り値と称する等）を置けない |
| `test_an_unknown_metric_is_never_zero_and_needs_a_reason` | 取れなかった値を 0 にすると「伸びていない」と誤読される。理由の無い unknown は直せない |
| `test_growth_must_name_delta_or_the_lifetime_average` | 増加速度が差分か参考の平均かを名前で区別する。「直近の伸び」に見える名前を語彙に入れない |
| `test_there_is_no_single_composite_score_anywhere` | 総合スコア・順位の欄を足せない（説明できない 1 つの数字に潰さない） |
| `test_the_format_is_the_requested_value_with_low_confidence` | 30 秒の動画を見て Shorts と断定する経路を契約で塞ぐ（形式は依頼の値・確からしさ low） |
| `test_the_audience_is_a_hypothesis_not_a_measurement` | 視聴者層を観測していないのに測定値として書けない |
| `test_references_need_plain_urls_and_youtube_channel_ids` | 危険な URL・形の違う channel id を参照に載せない（Fake の id を本物の形にした理由でもある） |
| `test_an_angle_must_cite_a_known_interpretation` | 根拠の無い企画の切り口を置けない |
| `test_the_interpretation_proposal_is_a_strict_llm_output_schema` | 実 LLM の strict な出力 schema にそのまま使える形で、`overall_score` や観測の欄を足した出力は schema 違反になる |

## `tests/unit/test_research_trend_rules.py`（純粋な規則）

| テスト | なぜ必要か |
|---|---|
| `test_the_delta_needs_at_least_two_observations_of_the_same_video` | 1 時点の観測から「伸び」を作らない（旧設計書の禁止事項） |
| `test_the_delta_uses_the_earliest_and_latest_observation` | 観測の順序に依らず、期間の両端で差分を取る |
| `test_an_unusable_delta_is_unknown_with_a_reason_never_zero` | 減少・欠損・同時刻の食い違い・tz なしで 0 や負の値を出さない |
| `test_the_lifetime_average_is_a_separately_named_reference_value` | 参考の平均が差分と別の名前で返る |
| `test_freshness_is_fresh_within_the_configured_window` | 窓は設定値（引数）で決まる。既定値を関数の中に再宣言しない（AGENTS.md §8） |
| `test_a_stale_trend_carries_its_date_until_the_stale_limit` | 古い Trend を日時つきで使い、上限を超えたら使わない |
| `test_no_trend_or_a_future_observation_is_none` | 時計ずれの未来の観測を fresh と誤認しない |
| `test_freshness_rejects_naive_times_and_a_non_positive_window` | tz なし・0 以下の窓という設定の誤りを黙って通さない |

## `tests/unit/test_research_trend_handler.py`（Handler の純粋な判断）

| テスト | なぜ必要か |
|---|---|
| `test_the_plan_is_deterministic_bounded_and_inside_the_window` | 再実行が同じ呼び出しキーに戻る前提（決定性）と、上限・依頼の期間を越えない計画 |
| `test_shorts_is_only_a_search_hint` | `shorts` は検索語のヒントで、長さの条件（videoDuration）を使わない |
| `test_trend_fetches_no_bodies_and_is_an_interpreting_handler` | Trend は取得の枠・費用を使わない。実行器が解釈の段を走らせる型であること |
| `test_no_observation_means_no_interpretation_call` | 観測が無いのに解釈器（有料になりうる）を呼ばない |
| `test_the_handler_rejects_an_evidence_request` | 種別の取り違えを黙って処理しない |
| `test_adoption_rejects_the_whole_proposal_on_any_violation` | 悪い提案を部分的に直して採用しない（修復しない。ADR-0014 の精神） |

## `tests/unit/test_research_trend_executor.py`（実行器 + Fake）

| テスト | なぜ必要か |
|---|---|
| `test_trend_runs_searches_and_one_interpretation_through_the_ledger` | 解釈 1 回が台帳の `assess` 1 行と一致する（予算外の呼び出しが無い）。取得の行が無い。成果物の解釈が実在の観測を指す |
| `test_the_delta_needs_the_same_video_observed_at_two_times` | 2 回の検索で同じ動画を別時点に観測したときだけ差分が出る。2 時点の観測そのものが事実として残る |
| `test_one_observation_time_never_yields_a_delta` | 同じ時刻の観測が重複しても差分を作らない |
| `test_missing_values_are_unknown_with_a_reason_and_shorts_is_not_inferred` | 非公開の登録者数を 0 にしない・観測に載せない。30 秒の動画に形式・長さを書かない |
| `test_a_rerun_reads_the_saved_proposal_and_does_not_interpret_again` | 成果物の前に落ちた再実行が解釈器を呼び直さない（二重課金の防止） |
| `test_a_bad_proposal_is_not_adopted_and_the_trend_is_partial` | 5 種類の悪い提案（存在しない観測・別の候補・スコア・参考値を直近の伸びと呼ぶ・存在しない解釈）を採用せず、合格にしない |
| `test_extra_fields_in_the_proposal_are_a_schema_violation` | 型に無い欄（総合スコア・観測の捏造）は実行器の schema 検査で落ち、恒久的な失敗として送り直さない |
| `test_without_an_interpreter_the_trend_is_observations_only_and_partial` | 解釈器が無い設定で解釈の無い Trend を `completed` にしない |
| `test_the_interpretation_is_bounded_by_the_assessment_ceiling` | retry も枠を数え、`max_assessments` を超える予約行を作らない（INV-36） |
| `test_a_failed_search_makes_the_trend_partial` | 一部の検索が落ちた Trend を完全な Trend として扱わない |
| `test_provider_none_blocks_a_trend_before_any_call` | Provider が `none` の Trend が外部を呼ばない（fail-closed） |

## `tests/unit/test_research_latest_trend.py`（B6 の読み口）

| テスト | なぜ必要か |
|---|---|
| `test_the_latest_completed_trend_is_returned_verified` | 新しい方を返し、中身が保存物と一致する |
| `test_only_completed_trends_of_the_same_channel_region_and_language_count` | `partial` や別チャンネル・別地域・別言語・別形式の Trend を企画に混ぜない |
| `test_a_tampered_or_missing_body_is_no_trend` | 改ざん・欠損した保存物を読まない（base INV-31 の考え方） |
| `test_a_newer_unverifiable_trend_does_not_fall_back_to_an_older_one` | 壊れた最新を隠して古い Trend を新しいものとして使わせない |
| `test_a_result_that_disagrees_with_the_current_artifact_is_no_trend` | 結果の記録と現行の成果物が食い違う依頼を信用しない |
| `test_any_failure_while_reading_is_no_trend` | DB の障害で呼び出し側（Planner）を止めない |
| `test_no_trend_at_all_is_none` | Trend が一度も無い初期状態で例外にならない |

## `tests/unit/test_research_trend_workflow.py`（worker の設定どおり）

| テスト | なぜ必要か |
|---|---|
| `test_the_registry_builds_the_fake_interpreter_only_for_fake` | `none` で解釈器を組まない（実 LLM は配線しない） |
| `test_a_fake_worker_runs_trend_to_a_completed_verified_artifact` | registry → Activity → Workflow → 実行器 → 読み口の配線が通る（B4 までの Trend は `handler_not_available` で止まっていた） |
| `test_a_none_worker_blocks_a_trend_without_calls` | 既定の `none` の worker が Trend で外部を呼ばない |

## `tests/unit/test_research_trend_fakes.py`（Tier A の Fake の拡張）

| テスト | なぜ必要か |
|---|---|
| `test_the_default_observation_time_is_unchanged` | 既定の観測時刻を変えていない（既存の Evidence / Tier A のテストの前提） |
| `test_stat_observed_at_and_view_counts_make_a_second_observation` | 差分の検査に要る 2 時点目を作れ、元のコーパスを変えない |
| `test_corpus_channel_ids_have_the_real_youtube_shape` | Fake の id が本物と違う形だと、Trend の契約が参照から落として検査が空振りする |

## 既存テストの変更

- `tests/unit/test_research_registry.py::test_exactly_the_evidence_and_trend_handlers_are_registered`
  （`test_only_the_evidence_handler_is_registered` を置き換えた。ADR-0039 §6）。
- `tests/architecture/test_research_isolation.py` の対象に `contracts/research_trend.py` を足した（広がる側）。
