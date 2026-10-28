# テスト設計の根拠: マーカー照合の3状態（ADR-0075、INV-109）

守るもの: 照合が「確かめられなかった」ときに、既に投稿済みの動画があるのに2本目を `videos.insert` しないこと
（AGENTS.md §9「provider 予約が未照合のまま再送しない」）。実 YouTube には出ない（`FakeVideoUploader` と
`httpx.MockTransport` だけ）。レビューで見つかった実際の穴: 旧 `_lookup_marker` は例外を握りつぶして `None` を返し、
`_reserve` が「無い」と読んで投稿していた。

## `tests/unit/test_upload_marker_lookup.py`

| テスト | 水準 | なぜ必要か / 落ちたら何が起きているか |
|---|---|---|
| `test_marker_lookup_states_are_three_and_distinct` / `test_marker_lookup_shape_is_validated` | 契約 | 3状態が型で別物であること。video id の無い FOUND や、理由付きの NOT_FOUND を作れない。壊れると UNKNOWN が NOT_FOUND に畳まれる入口になる |
| `test_adapter_exhausting_the_playlist_is_not_found` | adapter | 読み切った「無い」は NOT_FOUND。これが無いと全部 UNKNOWN になり、承認済みの再投稿が永久に進まない |
| `test_adapter_page_limit_with_more_pages_is_unknown_not_not_found` | adapter | ページ上限で打ち切ったのに「無い」と読むと、古いチャンネルで投稿済みの動画を見落として2本目を作る |
| `test_adapter_found_beyond_the_first_page` | adapter | 2ページ目以降の一致を FOUND にする（退行防止） |
| `test_adapter_malformed_playlist_page_is_unknown` / `test_adapter_malformed_videos_response_is_unknown` | adapter | `items` が欠けた・配列でない応答を「空」と読むと、API の不具合が「無い」になる |
| `test_adapter_http_failure_raises_instead_of_reading_as_not_found` | adapter | HTTP 失敗は例外のまま（呼び出し側が UNKNOWN にする）。None を返す実装への退行を止める |
| `test_lookup_error_before_round_two_never_inserts`（7 種の例外） | Activity | 本題。transient / quota / rate limit / auth / timeout / 想定外の例外のどれでも、session も動画も増えず、round 2 の予約も作られない。Activity の実コード・実台帳（SQLite）・fake YouTube を通す。単体の関数では「予約を作らない」ことは検査できない |
| `test_malformed_or_partial_lookup_before_round_two_never_inserts` | Activity | adapter が返す UNKNOWN（例外ではない）経路と、fake のページ打ち切り |
| `test_error_message_names_the_state_and_carries_no_secret_or_response` | Activity | UNKNOWN の理由に型名だけを出し、例外文（URL・token になりうる）を運ばない（INV-20 の趣旨） |
| `test_one_failed_attempt_is_unknown_even_if_the_next_one_finds_nothing` | Activity | 「最後の1回が成功の NOT_FOUND」で打ち消さない保守的な規則を固定する。緩めるなら ADR を変える |
| `test_empty_search_success_inserts_exactly_once` | Activity | UNKNOWN を恐れて NOT_FOUND でも止まる実装を止める。読み切って無ければちょうど1本投稿 |
| `test_found_adopts_the_video_without_any_insert` | Activity | FOUND は投稿ゼロで既存 video id を採用 |
| `test_repeated_executions_after_an_unknown_make_at_most_one_video` | Activity | retry / worker 再起動を模して失敗を3回、復旧後に3回実行しても動画は1本以下。復旧後は台帳が spent なので照合に行かない |
| `test_lookup_error_after_bytes_were_sent_stays_a_hold_and_opens_no_session` | Activity | bytes 送信後の session 失効の経路（`_reconcile_by_marker`）も、照合失敗で session を開かない |
| `test_round_one_never_looks_up` | Activity | 初回は照合しない（API を無駄に呼ばない・初回投稿が照合障害で止まらない） |

fake 側の変更（`tests/support/*`）: `lookup_video_by_marker` を追加しただけ。既存の `find_video_by_marker` の挙動は同じ。
