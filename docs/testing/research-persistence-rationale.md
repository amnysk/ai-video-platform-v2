# テスト設計の根拠: Research の契約と永続化（ADR-0037 / INV-36 / INV-37）

各テストが**なぜ必要なのか**を残す。守るものは 3 つある。(1) 調査の外部呼び出しが依頼ごとの上限を
超えないこと（お金と quota。INV-36）。(2) Research が本番の表と課金コードに触れず、Episode 本番工程が
Research に依存しないこと（INV-37）。(3) 依頼・状態・成果物の記録が、再送・再実行・途中停止のどれが
起きても半端な状態を作らないこと。実際に起きた事故から作ったテストではなく、旧ブランチの設計
（owner-XOR）を採らなかった結果として生じた境界から作ったテストである。そのため、各テストについて
「落ちたら何が起きているか」を書く。

## `tests/unit/test_research_contracts.py`（契約）

| テスト | なぜ必要か / 落ちたら何が起きているか |
|---|---|
| `test_vocabularies_are_research_only_and_do_not_touch_production_states` | research の語彙が本番の `JobType` / `ArtifactType` / `ProviderCall` に混ざると、本番の CHECK の張り替えが要る（ADR-0037 が避けた変更）。語彙の全集合も固定する |
| `test_the_kind_discriminates_the_request_and_rejects_cross_kind_inputs` | Trend の入力を持つ Evidence 依頼、未知の kind、余計なキーを推測で受けると、意味の違う依頼が同じ扱いになる |
| `test_as_of_must_be_timezone_aware` / `test_time_window_must_be_ordered` | 基準時刻が曖昧だと `request_hash`（Trend の日付の枠）がホストのタイムゾーンで変わる |
| `test_evidence_claims_are_unique_after_normalization` | 表記揺れだけが違う同じ claim を 2 回調べると、枠を無駄に使う |
| `test_limits_have_defaults_and_ceilings` | 上限値そのものが暴走したり文字列の数を受けたりすると、INV-36 の上限が意味を失う |
| `test_every_call_kind_has_exactly_one_ceiling_from_the_frozen_limits` | 種別ごとの上限の定義を 1 箇所に固定する。検索エンジンごとに枠を持つと実質の上限が増える（旧実装の抜け道） |
| `test_submit_carries_an_idempotency_key_that_is_not_part_of_the_spec` | 冪等キーが依頼の意味に入ると、同じ依頼の再送が別の依頼として扱われる |
| `test_refresh_slot_is_the_utc_date_of_as_of` | Trend の hash はこの日付で丸める。ローカル日付だと JST の朝に前日と別の依頼になる |
| `test_result_artifacts_must_match_the_execution_status` / `test_usage_cost_is_a_decimal` | 成果物の無い `completed` や、成果物のある `blocked` を結果として表せないようにする。research 以外の型の成果物も拒否する。金額は float にしない |

## `tests/unit/test_research_domain.py`（純粋なドメイン）

| テスト | なぜ必要か |
|---|---|
| `test_request_id_is_derived_from_the_idempotency_key` | 再送のたびに別 ID ができると、同じ調査を二重に実行して二重に課金する |
| `test_research_status_table` / `test_terminal_statuses_have_no_outgoing_edges` / `test_a_queued_request_cannot_finish_without_running` | 状態表の全行を固定する。終端から出る辺があると、完了した調査が再実行される |
| `test_call_status_table_only_leaves_reserved` / `test_a_dispatched_call_can_never_be_abandoned` | 送ったかもしれない呼び出しを「送っていない」として扱うと、同じ入力を再送して二重に課金する（INV-15 と同じ扱い） |
| `test_request_hash_*`（5 件）と `test_equal_money_limits_hash_equally` | 再利用の判定の唯一の定義を固定する。誰が頼んだか・Episode の参照・claim の順序・表記揺れで hash が変わると再利用が効かず、上限・Provider 設定・意味が違うのに同じ hash になると、別の依頼の結果を使ってしまう |
| `test_request_hash_payload_is_json_ready` | 正準 JSON にできない値（datetime・Decimal）が残っていると、hash が実装の詳細に依存する |
| `test_research_objects_live_under_their_own_prefix` | research の成果物を Episode の `artifacts/` 配下に置かない。不正な id や sha を含むキーも作らない |

## `tests/unit/test_research_repositories.py`（依頼と成果物、SQLite）

| テスト | なぜ必要か |
|---|---|
| `test_create_is_idempotent_by_key_and_derives_the_id` | 同じ冪等キーの再送で 2 行目を作らない。保存する hash は payload から計算した値そのものであること |
| `test_same_key_with_a_different_meaning_is_a_conflict_and_changes_nothing` | キーの使い回しで保存済みの依頼を書き換えない |
| `test_limits_and_payload_are_frozen_as_json` | 上限は依頼を受けた時点で凍結する。後から環境変数を変えても実行中の依頼の上限は変わらない |
| `test_status_transitions_go_through_the_table` / `test_blocked_requires_a_reason_code` | リポジトリが表を迂回しないこと。`mark_running` の再実行は冪等であること。`blocked` は理由つきで、終端ではないこと |
| `test_completed_requires_its_artifact_to_be_recorded_first` / `test_result_must_describe_the_same_request_and_status` | 状態だけが先に進んだ `completed`（成果物が無い）を作らない。別の依頼の結果を書き込まない |
| `test_find_reusable_*`（2 件） | 再利用するのは、鮮度の窓の中にあり、`completed` で、現行の成果物を持つ依頼だけ。`partial` や実行中の依頼を再利用すると、未完成の調査を完成品として扱う |
| `test_list_unstarted_returns_old_queued_requests` | workflow の開始漏れを回収する手がかり（Worker の段で使う） |
| `test_recording_*` / `test_a_to_b_to_a_restores_the_old_row_as_current` / `test_artifact_types_do_not_supersede_each_other` | 世代管理が本番（ADR-0012）と同じ意味を持つこと。現行を 2 本持たないこと、型をまたいで降ろさないこと |
| `test_artifact_key_must_be_the_research_key_of_that_request` / `test_artifacts_need_an_existing_request` | Episode のキーや別の依頼のキーを記録すると、再利用の検証が別の実体を読む |

## `tests/unit/test_research_call_ledger.py`（呼び出し台帳、INV-36）

| テスト | なぜ必要か |
|---|---|
| `test_calls_get_consecutive_sequence_numbers_per_kind` | 番号は種別ごとの枠の中で 1 から振る |
| `test_the_ceiling_stops_the_next_reservation_before_insert` | 上限に達したら行を作らずに止める（呼ぶ前に止める） |
| `test_abandoned_and_spent_calls_still_count_and_seq_is_never_reused` | 手放した行や失敗した行が枠を返すと、失敗を繰り返すだけで上限を越えて呼べてしまう |
| `test_re_reserving_the_same_key_returns_the_same_call_and_uses_no_budget` / `test_same_key_for_a_different_call_is_a_conflict` | Activity を再実行しても枠を消費しない。キーの使い回しで別の呼び出しを隠さない |
| `test_calls_are_reserved_only_while_the_request_is_running` | 完了した依頼や blocked の依頼から外部呼び出しが出ないようにする |
| `test_a_dispatched_call_without_an_outcome_blocks_resending_the_same_input` | 成否が不明な呼び出しと同じ入力を自動で再送しない（その入力だけを止める） |
| `test_a_dispatched_call_cannot_be_abandoned_and_is_not_dispatched_twice` | 同じ予約で 2 回送らない。送った行は `spent` にしかならない |
| `test_money_budget_counts_every_call_that_may_have_cost` / `test_abandoned_calls_do_not_count_toward_money` | 金額と quota の上限（設定されていれば）は、費用が発生しうる行の見積りの合計で判定する |
| `test_the_database_rejects_a_duplicate_call_seq` | アプリの採番を迂回しても、同じ番号の 2 行目は DB が拒否する（最後の砦） |
| `test_a_racing_writer_does_not_push_the_count_past_the_ceiling` | 採番の読み取りと INSERT の間に別の書き手が割り込んでも、読み直して次の番号を取り、上限は越えない。SQLite では並行する 2 つのトランザクションを作れないので、競合を決定的に再現する。PostgreSQL での並行検査は未移植（INV-36 の注記） |

## `tests/contract/test_migration_0015_research.py`（migration を実際に適用した SQLite）

| テスト | なぜ必要か |
|---|---|
| `test_0015_follows_0014_and_freezes_its_vocabulary_as_literals` / `test_0015_vocabulary_matches_the_contracts_it_was_written_for` | 語彙を literal で凍結し、contracts から導出しない（0014 の規約）。contracts を後から変えても履歴の migration は変わらない。書いた時点の contracts と一致していたことも記録する |
| `test_upgrade_adds_only_research_tables_and_leaves_production_tables_alone` | 0015 が本番の 4 表の列と CHECK を 1 つも変えないこと（INV-37。owner-XOR を採らなかったことの検査） |
| `test_research_tables_only_reference_research_tables` | 本番の表への FK を張らない |
| `test_the_database_rejects_a_second_row_with_the_same_call_seq` / `test_the_database_rejects_invalid_ledger_rows` | INV-36 の DB 制約を、ORM ではなく migration が作った実際のスキーマで確かめる（番号 0、未知の種別・状態、dispatch 済みの abandoned、決着時刻の無い spent、負の費用） |
| `test_the_database_keeps_one_current_artifact_per_type` / `test_the_database_rejects_unknown_request_vocabulary` | 現行を 1 本に保つ部分一意索引と語彙の CHECK が、実際に効いていること |
| `test_downgrade_drops_the_empty_research_tables` / `test_downgrade_refuses_while_research_rows_exist` | 空なら戻せること。行があれば何も変えずに拒否すること（支出の記録を黙って消さない） |

## `tests/architecture/test_research_isolation.py`（INV-37）

| テスト | なぜ必要か |
|---|---|
| `test_the_research_persistence_modules_exist` | 検査の対象が空のまま自明に通るのを防ぐ |
| `test_research_does_not_import_production_billing_or_production_repositories` | research が `repositories.py` / `paid_job` / 本番の ORM 行を使い始めた時点で落ちる。そうなれば owner-XOR と同じ結合が戻ってくる |
| `test_research_code_does_not_name_production_tables` | 生 SQL や文字列の FK で本番の表に書く経路を塞ぐ（docstring は対象外） |
| `test_research_rows_only_reference_research_tables` / `test_production_rows_do_not_reference_research_tables` | ORM の側でも FK が両方向に分離していること |
| `test_the_episode_production_path_does_not_import_research` | production / render / upload / storyboard / pipeline / 課金が Research を import した時点で落ちる。本番が Research を待つ、あるいは Research のせいで失敗する経路をコードの上に作らない。企画・台本への opt-in 接続は、その段で既定 OFF の検査とともに扱う |
