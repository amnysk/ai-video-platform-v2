# fal 残高不足への耐性（ADR-0057 / INV-71〜74）のテスト設計根拠

事故の形: fal の残高が尽きるとアカウントがロックされ、401/402/403 で submit が拒否される。
旧実装はこれを (1) 認証 incident（10分窓で抑止が解ける）、(2) 入力の拒否（入金後も恒久ブロック）、
(3) 課金された前提の確定（費用の過大計上）として誤記録し、本文を捨てるので原因も後から分からない。
本番には再送してはいけない未照合予約（`591d1a8b…`）も残っている。
fake / `httpx.MockTransport` だけを使い、fal・YouTube には一切到達しない。

## 分類と本文要約（`tests/unit/test_fal_refusal.py`、unit）

| テスト | 守るもの / 落ちたら何が起きているか |
|---|---|
| `test_status_alone_never_decides_balance` | 402/403 でも本文の印が無ければ残高不足と確定しない。公式文書に残高の構造化コードは無く、status だけで決めると鍵の権限エラーを「入金待ち」と誤認する |
| `test_balance_marker_beats_status_for_402_and_403_and_401` | 印があれば status に依らず残高不足。403 で出た本番の観測を 402/401 でも取りこぼさない |
| `test_401_without_balance_marker_is_auth_invalid` | 印の無い 401 は従来どおり認証（ADR-0030 の経路）。残高に寄せすぎない |
| `test_three_statuses_map_to_three_different_kinds` | 401/402/403 がそれぞれ別の種類になる（要件: 異なる理由 → 異なる kind） |
| `test_markers_live_in_one_place_with_provenance` | 判定語は1表だけ・出所つき（AGENTS.md §8）。出所の無い印を足せない |
| `test_structured_and_list_bodies_are_searched` | `detail` が文字列・配列・入れ子の `error.message` のどれでも印を見つける |
| `test_summary_redacts_secrets_urls_and_truncates` | 偽の API キー・Authorization・署名付き URL（`?token=` / `X-Amz-Signature`）が要約に残らない。長さの上限 |
| `test_summary_of_unreadable_body_is_explicit` | 本文が読めないことを「空」ではなく明示する（原因不明を捏造しない） |
| `test_submit_refusals_use_distinct_error_types` | queue の submit が3種の例外に分かれる。残高/分類不能は `ProviderUnavailableError` の子ではない（認証 incident に数えない） |
| `test_401_diagnostic_body_is_kept_in_the_message` | G0: 以前は捨てていた 401 の本文が診断として残る |

既存テストの変更（理由: ADR-0057 で設計が承認された仕様変更）:
`tests/unit/test_fal_queue.py` の「本文なし 403」と `tests/unit/test_fal_storage.py` の 403 は、
`ProviderUnavailableError` ではなく `ProviderRefusalUnknownError` を期待するようにした
（本文に印が無い 403 を認証と決め打たない = ADR-0030 の「原因を断定しない」の趣旨を、安全側の
`UNKNOWN`（breaker を開く）へ進めた）。401 は変わらない。

## breaker と保留の永続化（migration 0018）

### unit: SQLite（`tests/unit/test_provider_health_repo.py`）

| テスト | 守るもの / 落ちたら何が起きているか |
|---|---|
| `test_default_snapshot_is_closed` | 行が無いアカウントは closed。日次の入口ゲートが初回から止まらない |
| `test_open_is_idempotent_and_keeps_the_first_reason` | 2件目以降の拒否は行を増やさず、最初の理由・開いた時刻を保ち、観測した拒否数だけ進む（原因の上書き・通知の嵐を防ぐ） |
| `test_repeated_opens_leave_exactly_one_row_and_one_winner` | 開いた勝者は1回だけ（逐次。真の並行は下の integration）。unit の SQLite は接続を共有するため並行性の証明にならず、gather では行が消えた（これは検査基盤の限界で、実装の欠陥ではない） |
| `test_breaker_never_closes_with_time` | 30日前に開いた breaker も open のまま。`record_accepted` でも open は閉じない（承認が先。INV-72） |
| `test_accounts_are_independent` | アカウント鍵が違えば独立（将来のチャンネル別アカウント） |
| `test_half_open_needs_open_then_accepted_submit_closes_it` | closed から half_open にはならない。half_open 中に受理された submit だけが閉じる。承認の指紋・実行者が残る |
| `test_refusal_in_half_open_reopens` | 再開で再び拒否されたら open に戻る（開いた勝者として数える） |
| `test_close_requires_open_or_half_open` | closed を closed にしない。閉じた後の再オープンも効く |
| `test_hold_is_idempotent_and_records_snapshot` | 同じ (episode, scene, account) の保留は1行。その時点の予約 id が写される |
| `test_holds_for_different_scenes_share_one_held_episode` | シーンごとの行があっても保留 Episode 数は Episode 単位（日次の `MAX_HELD_EPISODES` の数え方） |
| `test_release_frees_the_slot_and_a_new_hold_can_be_recorded` | 解放後の再停止で新しい保留を作れる（部分一意索引が解放済みを数えない） |
| `test_held_episodes_come_oldest_first` | 「古い順 N 件」の再開の根拠 |

### integration: 実 PostgreSQL（`tests/integration/test_provider_breaker_postgres.py`）

| テスト | 守るもの |
|---|---|
| `test_simultaneous_opens_leave_one_row_and_exactly_one_winner` | 8 Episode が別接続で同時に拒否されても breaker は1行・開いた勝者は1人（ADR-0057: アプリ側ロックを作らず、一意制約と条件付き UPDATE だけで足りる） |
| `test_simultaneous_holds_for_the_same_target_make_one_row` | 同じ保留の同時記録は1行（部分一意索引） |

### contract（`tests/contract/test_provider_health_migration.py`）

| テスト | 守るもの |
|---|---|
| `test_release_preflight_scanner_finds_nothing_in_0018` | release preflight の migration 走査（`scan_migration`）が0件。制約の drop・型変更・NOT NULL 追加が混ざると release gate が止まる |
| `test_0018_upgrade_only_creates_tables_and_indexes` / `test_0018_creates_only_its_own_tables` | expand-only の固定。`provider_reservations` の CHECK を広げる、のような既存表への変更を紛れ込ませない |

## PaidJobRunner の残高拒否・breaker・保留（`tests/unit/test_paid_job_balance.py`、unit: SQLite + fake）

本物の `PaidJobRunner`・台帳・breaker を通し、provider は fake（`submit_error` / `prepare_error` を注入）。

| テスト | 守るもの / 落ちたら何が起きているか |
|---|---|
| `test_balance_refusal_is_not_accepted_zero_cost_and_opens_the_breaker` | **事故の核**。残高拒否が `spent`+`not_accepted`・費用 0・`input_rejected_by_provider=False` で確定し（G2/G3）、認証 incident に数えられず（G1）、breaker が開き、保留に episode/scene/理由/生成 profile/予約 id の写しが残る |
| `test_unknown_refusal_opens_the_breaker_with_the_unknown_hold_reason` | 分類不能の拒否も安全側で止まり、保留の理由は「確認待ち」（残高不足と断定しない） |
| `test_after_the_top_up_the_same_input_is_submitted_again` | 入金（breaker を承認で閉じる）後、同じ input_hash が新ラウンドで送れる。以前は `ProviderRejectedRetryBlockedError` で恒久ブロック |
| `test_open_breaker_stops_before_any_reservation_for_every_fal_provider` | open の間は `fal_image` / `fal_video` とも予約・prepare・submit が起きない（台帳に何も残らない）。他の Episode も保留として記録される |
| `test_open_breaker_does_not_block_awaiting_an_already_accepted_job` | 受理済みジョブの await/再開（再 submit ではない）は breaker で止めない（課金済みの結果を取りこぼさない） |
| `test_prepare_refusal_opens_the_breaker_and_leaves_no_reservation` | 非課金の準備（upload）の拒否でも同じ（予約の前なので台帳は空） |
| `test_accepted_submit_in_half_open_closes_the_breaker` | 承認済みの再開で受理された submit だけが half_open を閉じる |
| `test_ambiguous_submit_stays_unreconciled_and_is_never_resent` | 送信後の切断は曖昧: 未照合・費用 unknown・breaker は開かず、2回目の submit は `UnreconciledReservationError` で止まり provider へ再送しない |
| `test_non_fal_providers_are_not_gated` | fal 以外（YouTube など）は breaker の影響を受けない |
| `test_not_accepted_refusal_adds_nothing_to_the_recovery_cost` | 復旧費用の上限（INV-34 / ADR-0035）が、受理されなかった拒否の見積額で食われない |

### 費用区分（`tests/unit/test_provider_cost_state.py`、純粋関数）

| テスト | 守るもの |
|---|---|
| `test_not_accepted_is_not_charged_and_counts_zero` | 受理されなかった拒否は課金として数えない（G3） |
| `test_the_five_states_are_distinguished` | 見積り済み / 受理 / 課金確認 / 課金前提 / 不明が行から区別できる |
| `test_ambiguous_and_accepted_count_at_the_estimate_but_estimated_does_not` | 曖昧・受理は見積額を安全側で数える（予算判定が過小にならない）。未 dispatch の予約は数えない |

既存テストの変更: `tests/unit/test_log_provider_calls.py` の本文なし 403 の例外型（`ProviderUnavailableError` →
`ProviderRefusalUnknownError`。ADR-0057）。ログの `error_category=access_denied` / `http_status_only` は変えていない。

### 費用の集計（`tests/unit/test_production_metrics_script.py`）

| テスト | 守るもの |
|---|---|
| `test_not_accepted_refusals_do_not_count_as_cost` | `scripts/production-metrics.py` の費用合計が、受理されなかった拒否（`reconciled_by=not_accepted`）の見積額を数えない（G3: 過大計上の解消） |

## 再開 CLI（`tests/unit/test_provider_credits_script.py`、unit: SQLite + fake の再開 API）

| テスト | 守るもの / 落ちたら何が起きているか |
|---|---|
| `test_status_is_read_only_and_reports_the_hold` | status は何も書かない。breaker・保留 Episode・予算上限が見える |
| `test_half_open_needs_a_structured_approval_and_evidence` | 承認 token なし・形式不正・根拠なしでは breaker を動かせない（口頭の「入金した」だけで再開しない） |
| `test_half_open_is_dry_run_by_default_and_never_prints_the_token` | 既定 dry-run。承認 token 本体は出力・DB に残らず指紋だけ |
| `test_resume_is_refused_while_the_breaker_is_open` | open のまま再開できない（先に承認つきで half-open） |
| `test_resume_is_refused_for_the_production_unreconciled_reservation_state` | **本番の `591d1a8b…`（fal_video・dispatched・provider 参照あり・未照合）と同じ状態**の Episode は再開を拒否し、再開 API を呼ばず、保留も解放しない。二重課金の最後の砦 |
| `test_resume_is_refused_over_budget_or_without_a_cap` | 月予算を超える・上限未設定の再開は拒否（課金を伴う再開を上限なしで通さない） |
| `test_resume_concurrency_is_capped` | 明示 id でも `--oldest` でも `RESUME_MAX_CONCURRENCY`（1）を超えない |
| `test_a_resumed_episode_still_running_counts_toward_the_cap` | 再開済みでまだ実行中の Episode も同時数に数える（連続で1件ずつ投入して実質並列にならない） |
| `test_oldest_picks_the_first_held_episode_and_uses_the_existing_resume_path` | 古い順 N 件。再開は既存の再開 API の呼び出しだけで、breaker は触らない（half_open のまま受理された submit が閉じる） |
| `test_resume_dry_run_changes_nothing` | dry-run は保留も再開 API も動かさない |
| `test_failed_resume_restores_the_hold` | 再開 API が拒否したら保留を元に戻す（保留の無い blocked を作らない） |
| `test_only_held_blocked_episodes_can_be_resumed` | 保留の無い Episode を、この CLI から再開できない |
| `test_succeeded_scenes_are_reused_after_resume_not_regenerated` | 再開後の production は、成功済みシーンを再利用ゲートで再利用する（provider に何も送らない） |
| `test_api_resume_client_calls_only_the_existing_resume_endpoint` | 再開クライアントは `POST /episodes/{id}/resume` だけを呼び、202 以外は拒否として扱う |
| `test_script_has_no_network_or_provider_imports` | スクリプト自体は fal に接続しない（残高の確認は所有者の attestation） |

## Temporal 側（`tests/unit/test_production_balance_workflow.py`、time-skipping サーバ + 本物の `raise_activity_error`）

Workflow コードは変えていない。残高拒否の例外は `NeedsInputError` なので、既存の「non_retryable → 型名で needs_input →
`record_failure` で blocked → workflow が終わる」経路に乗る。それを固定する。

| テスト | 守るもの / 落ちたら何が起きているか |
|---|---|
| `test_balance_refusal_ends_the_workflow_blocked_without_a_retry_storm` | 1 submit = 1 attempt（Activity の retry も workflow の再 submit も無い）。結果は `blocked` + `needs_input`、失敗は1回だけ記録 |
| `test_an_open_breaker_ends_the_workflow_the_same_way` | breaker open（`ProviderBreakerOpenError`）も同じ終わり方 |
| `test_a_seven_day_hold_costs_no_more_activity_and_no_more_history` | **7日保留**: 終了した workflow は待機せず、7日進めても履歴は増えず（< 80 イベント）、再 submit も無い（待ち続ける workflow が履歴を伸ばす設計を排除） |

2シーン（成功済み + 拒否）の workflow 試験は、兄弟 Activity の cancel を mock が受けられず試験サーバが終わらない
（`test_production_scene_recovery_workflow.py` の既知の制約）ので置いていない。成功済みシーンを再送しない性質は
再利用ゲートの試験（`test_succeeded_scenes_are_reused_after_resume_not_regenerated`）で見る。

## 再起動・並行（追加分）

| テスト | 守るもの |
|---|---|
| `test_breaker_and_holds_survive_a_process_restart`（unit, ファイル SQLite） | 接続を捨てて作り直しても、7日前に開いた breaker は open のまま・保留は残る（breaker が worker のメモリではなく DB にある） |
| `test_concurrent_episodes_hitting_balance_exhaustion_open_one_breaker_and_double_nothing`（integration, 実 PostgreSQL） | 6 Episode が同時に残高拒否に当たっても breaker は1行、予約は (episode, scene) ごとに高々1つで全て `not_accepted`、provider へ送った回数 = 予約数、開いた後の submit は予約を作らない。全 Episode に保留が残る |
| `test_open_hold_reader_folds_holds_per_episode_oldest_first` | ADR-0058 の watchdog 用 port: Episode 単位・最初の保留が代表・解放した Episode は消える |
| `test_open_hold_record_matches_the_daily_gate_contract_once_merged` | `contracts/daily_gate.py` がある branch（統合後）でだけ走り、返す形が `OpenHold` と一致することを確かめる |
| `test_a_daily_gate_hold_at_storyboard_ready_resumes_from_production` | 日次ゲートの保留（`storyboard_ready`・`scene_id=NULL`）も `production` から再開できる |

## 拒否本文の要約・分類の硬化（review-E P2-5 / P2-6）

| テスト | 守るもの |
|---|---|
| `test_summary_drops_query_strings_of_every_url_shape`（`test_fal_refusal.py`） | 署名付き URL の query は scheme なし（`//host/f?sig=`）・裸 host（`host.tld/f?sig=`）でも署名を持つ。`https://` だけを見る旧パターンは ledger / breaker evidence に署名を漏らした。 |
| `test_summary_keeps_a_question_mark_in_plain_prose` | 反対側: ドットの無い普通の文の `?` まで URL とみなして本文を壊さない。 |
| `test_balance_marker_ignores_whitespace_differences` | 印が空白 2 個・改行・タブで割れても `BALANCE_EXHAUSTED`（status だけでは決めない方針は不変。印が無ければ従来どおり UNKNOWN）。 |
| `test_refusal_body_echoing_the_api_key_is_redacted`（`test_fal_storage.py`） | storage の拒否が `secrets=` を渡さず、パターンに合わない形で本文にエコーされた key が例外・ledger に残りえた。 |
