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
