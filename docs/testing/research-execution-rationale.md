# テスト設計の根拠: Research の Gateway・実行器（ADR-0037 §5 / §6 / §8、INV-36 / INV-37）

各テストが**なぜ必要なのか**を残す。B2 のテストが守るものは 4 つある。

1. 外部呼び出しはすべて台帳を通り、上限を越えない（お金と quota。INV-36）。
2. 同じ呼び出しを二重に送らない。成否が不明な呼び出しは送り直さない（ADR-0013 / INV-15 と同じ規律）。
3. 設定が無い・予算が無い依頼は、外部を呼ぶ前に止まる（fail-closed。ADR-0037 §6）。
4. 再利用・記録する成果物は、本体を読み戻して検証済みである（base INV-31 の考え方。ADR-0037 §5）。

実際に起きた事故から作ったテストではない。旧ブランチ（`claude/research` 8cede47）の Gateway・実行器の
意味を、B1 の新しい永続化（`research_calls` / `research_artifacts`）の上で作り直したときに生じた境界から
作った。そのため、各テストについて「落ちたら何が起きているか」を書く。Provider は Fake（固定コーパス）だけで、
実ネットワークと有料 API には出ない（AGENTS.md §9）。

## `tests/unit/test_research_execution_domain.py`（純粋な部分）

| テスト | なぜ必要か / 落ちたら何が起きているか |
|---|---|
| `test_a_real_provider_needs_both_money_and_quota_limits` | 実 Provider で金額か quota の**どちらか一方でも**未設定なら止める。旧実装は文章（両方）とコード（どちらか）が食い違っていた。緩い側に戻ると、上限の無い有料呼び出しができてしまう |
| `test_admission_blocks_an_unconfigured_provider_before_the_budget` | `none` は予算より先に `provider_not_configured` で止める。理由コードが入れ替わると、運用者が直すべきもの（Provider の選定か上限か）を取り違える |
| `test_the_plan_is_cut_at_the_ceiling_and_the_rest_is_reported_skipped` | Handler が上限を超えて計画しても、実行器が切る（Handler を信用しない）。切った分は `partial` の理由として残る |
| `test_a_plan_with_duplicate_or_malformed_step_ids_is_rejected` | 段の ID は警告と成果物に写る。重複すると 2 つの検索の結果が区別できない |
| `test_fetch_targets_are_deduplicated_by_normalized_url_and_capped` | 表記だけが違う同じ URL を 2 回取得すると取得枠を浪費する。上限を超えて選んだ分も落とす |
| `test_normalize_url_folds_case_default_ports_and_fragments_but_keeps_the_query` | 重複判定の唯一の定義。query を並べ替えると意味の違う資料を同じ扱いにする |
| `test_raw_provider_output_lives_under_the_research_prefix` | 生データを Episode の `artifacts/` に置かない。不正な ID でパスを作らない |
| `test_every_research_error_lives_in_the_one_research_errors_module` | research の型名の表は `domain/research/errors.py` の import 時に作る。別のモジュールに例外を足すと表に載らず、Temporal の分類で `needs_input` に落ちる |
| `test_every_research_error_is_reachable_by_type_name_with_its_isinstance_class` | 型名による分類（Temporal）と isinstance による分類が一致すること。permanent な research の例外が retry されないこと |
| `test_research_type_names_do_not_shadow_the_base_table_and_it_stays_unchanged` | `domain/errors.py` の表に research の型を足さない（ADR-0037 §8.4）。名前が衝突すると、どちらの表で引くかで分類が変わる |
| `test_the_research_lookup_falls_back_to_the_base_table` | research の表に無い名前は基底の表で引き、未知の名前は `needs_input`（INV-12） |

## `tests/unit/test_research_registry.py`（Provider の組み立て）

| テスト | なぜ必要か |
|---|---|
| `test_the_default_provider_is_none_and_builds_nothing_that_can_call_out` | 既定の `none` で外部を呼べる Provider を組むと、設定の無い環境で調査が外に出る |
| `test_fake_builds_the_fixed_corpus_providers` | `fake` は固定コーパスの Fake だけを組む |
| `test_only_fake_and_none_are_accepted` | 実 Provider の名前を設定値として受けない（選定は所有者の判断と ADR を待つ） |
| `test_unknown_modes_count_as_real_and_unconfigured` | 将来の設定値が登録前に紛れ込んでも、予算の門と Provider の門の両方で止まる側に倒れる |
| `test_the_provider_config_version_separates_fake_results_from_other_modes` | Fake の結果が別の Provider 設定の依頼に再利用されると、本物の調査の代わりに固定コーパスの結果を使ってしまう |
| `test_exactly_the_evidence_and_trend_handlers_are_registered` | B2 には種別ごとの Handler が無く、この欄は `test_no_kind_specific_handler_is_registered_yet` だった。B4（ADR-0038 §6）で Evidence を登録して `test_only_the_evidence_handler_is_registered` に、B5（ADR-0039 §6）で Trend を登録してこの名前に置き換えた。登録される種別が**ちょうど** Evidence と Trend であること（黙って別の種別が増えない・どちらかが抜けない）と、種別と Handler の対応を検査する。登録されていない種別の `blocked` は `tests/unit/test_research_executor.py::test_a_kind_without_a_handler_is_blocked` が引き続き検査する |
| `test_the_cost_model_estimates_youtube_quota_from_the_shared_constant` | quota の見積もりは Adapter と Fake が共有する 1 つの定数から来る。別の値を書くと上限の判定がずれる |

## `tests/unit/test_research_gateway.py`（受け付け・鮮度キャッシュ・予算の門・再開）

| テスト | なぜ必要か |
|---|---|
| `test_submit_is_idempotent_by_key` / `test_same_key_with_a_different_meaning_is_a_conflict` | API の再送で 2 つ目の依頼を作ると二重に調査して二重に課金する。キーの使い回しで保存済みの依頼を書き換えない |
| `test_unset_money_limits_are_filled_from_settings_and_frozen` | 上限を指定しない依頼には設定値を凍結する。明示された上限を設定値で上書きしない |
| `test_a_fresh_completed_request_with_the_same_meaning_is_reused` | 同じ意味の調査を鮮度の窓の中で繰り返さない（枠とお金の節約） |
| `test_reuse_is_rejected_when_the_stored_object_no_longer_matches` / `test_reuse_is_rejected_when_the_stored_object_is_missing` | 記録の sha256 と本体が食い違う・本体が無い成果物を再利用すると、壊れた調査結果を検証済みとして下流へ渡す（base INV-31 の考え方）。その場合は新しい依頼として実行する |
| `test_reuse_respects_the_freshness_window_per_kind` | Trend の 24 時間の窓を越えた古い結果に固定されない |
| `test_a_blocked_request_is_never_reused` | 止まった依頼を完了品として再利用しない |
| `test_provider_none_blocks_with_a_recorded_reason_and_no_call` | `none` の依頼は理由コードつきで `blocked` になり、台帳に行が 1 つも無い。開始した記録（`started_at`）は残る |
| `test_a_real_provider_without_money_and_quota_limits_is_blocked_before_any_call` / `test_a_real_provider_with_both_limits_is_queued` | 実 Provider の代役（設定だけで「実物」を表す）で、上限が片方でも欠ければ呼ぶ前に止まり、両方あれば通る |
| `test_resume_refuses_a_request_whose_frozen_limits_still_lack_budget` | 上限は依頼に凍結されているので、再開しても直らない。再開して同じ理由で止まるのを繰り返さない |
| `test_resume_requeues_once_the_cause_is_fixed` / `test_resume_of_an_unknown_request_is_none` | Provider を設定した後は再開できる。二度目の再開は何もしない |
| `test_the_submitted_payload_is_the_contract_spec` | 冪等キーを依頼の意味（payload）に入れない |

## `tests/unit/test_research_executor.py`（実行器）

| テスト | なぜ必要か |
|---|---|
| `test_every_external_call_goes_through_the_ledger_and_the_request_completes` | 検索・取得のすべてが台帳に `spent` で残り、dispatch の記録がある。台帳を通らない呼び出しがあると上限（INV-36）が効かない。URL の取得は注入された fetcher だけを通る |
| `test_the_artifact_is_written_under_the_research_key_read_back_and_recorded` | 成果物は research のキーに置き、本体の sha256 が記録と一致し、結果が同じ成果物を指す |
| `test_re_executing_a_finished_request_returns_its_outcome_without_calling` | 終わった依頼の再実行（Activity の再実行）で呼び出しを送らない |
| `test_provider_none_blocks_before_any_call` / `test_a_real_provider_without_budget_is_blocked_by_the_executor_too` | Gateway を経由しない依頼も、実行器の開始で同じ門を通る（判定は 1 か所の関数） |
| `test_a_kind_without_a_handler_is_blocked` | Handler の無い種別を空の成果物で `completed` にしない |
| `test_searches_planned_beyond_the_ceiling_are_not_sent_and_the_result_is_partial` | 上限を超えた計画は送らず、`partial` と警告で残す |
| `test_reaching_the_ledger_ceiling_stops_the_calls_and_finishes_partial` | retry で使った番号も枠を数える。台帳の上限に達したら行を作らずに止め、`partial` にする（INV-36 を実行器の側から確かめる） |
| `test_the_quota_budget_is_estimated_before_the_call` | quota の上限は呼ぶ前の見積もりで判定し、超える呼び出しを送らない |
| `test_a_transient_failure_raises_retryable_without_leaving_a_reservation` | 一時障害は retryable を投げ、予約を `reserved` のまま残さない（成否不明の行を作らない）。retry では成功済みの検索を生データから読み、送り直さない |
| `test_a_transient_fetch_failure_is_retried_in_a_new_round` | 値で返る取得の一時障害（timeout）も同じ扱い。新しい番号で取り直す |
| `test_an_ambiguous_call_is_not_resent_and_blocks_the_request` | dispatch 済みで結果の無い呼び出し（送信後のクラッシュ）を送り直すと二重課金になる。解放もせず、依頼を `blocked`（`ambiguous_call`）にする |
| `test_a_call_whose_raw_output_was_saved_is_settled_without_resending` | 呼んで生データを保存した後・`spent` の前に落ちた場合は、保存物から続ける |
| `test_an_unclassified_error_leaves_the_call_ambiguous_and_it_is_never_resent` | 分類できない例外を握りつぶして `spent` や `abandoned` にしない。呼んだかもしれないので、再実行は送り直さずに止まる |
| `test_a_permanently_rejected_search_is_not_resent_when_the_request_is_retried` | 同じ入力で必ず同じ結果になる失敗を retry のたびに送り直すと、枠を無駄に使う |
| `test_quota_rate_limit_and_auth_stop_the_calls_and_block_when_nothing_is_usable` / `test_a_stop_after_usable_results_finishes_partial` | quota・rate limit・認証の拒否の後で呼び続けない。使える結果があれば `partial`、無ければ理由コードつきの `blocked` |
| `test_a_permanently_failed_fetch_is_a_gap_and_the_result_is_partial` | 取得できない資料は穴として残し、`completed` にしない。同じ URL を送り直さない |
| `test_the_deadline_stops_further_calls` | 依頼に凍結した期限を過ぎたら呼ばない |
| `test_a_readback_mismatch_records_nothing_and_is_retryable` | 読み戻しが合わない成果物を現行にしない。retry では呼び出しを送り直さずに保存からやり直す |
| `test_record_failure_classifies_by_the_research_type_name` | retry を使い切った失敗の記録は research の型名の表で分類する（基底の表だけでは research の型が分からない）。二度記録しても状態を書き換えない |

## `tests/unit/test_research_raw_store.py`（生データ）

| テスト | なぜ必要か |
|---|---|
| `test_search_results_round_trip_exactly` / `test_fetched_content_round_trips_including_failures` | 再実行は保存した生データを「呼んだ結果」として使う。往復で値が変わると、再実行が別の結果から成果物を組む |
| `test_the_evidence_of_a_call_cannot_be_rewritten` | 「呼んだ証拠」を別の内容で上書きできない |

## `tests/architecture/test_research_isolation.py`（B2 で追加した検査）

| テスト | なぜ必要か |
|---|---|
| `test_the_research_execution_modules_exist` | 検査の対象が空のまま自明に通るのを防ぐ |
| `test_research_execution_does_not_wire_a_real_provider` | registry・Gateway・実行器が `HttpContentFetcher` / `UrlGuard` / `YouTubeSearchProvider` を組んだ時点で落ちる。実 Provider の配線は所有者の判断と ADR を待つ（ADR-0037 §6） |

## 検査していないこと

- 本物の並行実行（2 つの Worker が同じ依頼を同時に実行する）。SQLite では並行する 2 つのトランザクションを
  作れない。台帳の DB 制約が最後の砦であることは B1 のテストが確かめている。PostgreSQL での検査は B3
  （Worker）の段で足す。
- Temporal の retry そのもの。ここでは `execute` をもう一度呼ぶことで retry を再現している。
