# JP-MAS-002 P2-B（バックエンド）のテストの理由（ADR-0045）

テストを足した理由を、足したのと同じコミットで残す。

## 1. 一覧の複合 cursor と絞り込み（ADR-0045 §5）

- `tests/unit/test_episode_list_cursor.py::test_same_timestamp_episodes_across_pages_lose_and_duplicate_nothing`:
  `created_at` だけの cursor は同時刻の Episode をページ境界で落とす（ADR-0044 残リスク 3）。
  同一時刻の 5 件を 2 件ずつ辿り、欠落も重複も無いことを固定する。
- `test_without_before_id_the_old_created_at_only_cursor_still_works`: `before_id` の無い旧 client の動作を保つ。
- `test_filter_by_status` / `test_q_is_a_case_insensitive_substring_and_wildcards_are_literal`:
  `status` 絞り込みと `q`（大文字小文字無視の部分一致）。`%` `_` が wildcard として効くと
  「100%」が「1000」にも当たるので、文字として扱うことを固定する。
- `test_request_validates_q_length_and_defaults_are_backward_compatible`: 追加フィールドは全て任意。
- `tests/contract/test_application_operations_http.py::test_list_episodes_takes_before_id_status_and_q_as_query`:
  HTTP の query が `ListEpisodesRequest` に渡り、不正な status・id は 422。
- 既存 fake（`tests/unit/test_application_service.py` の `list_episodes`）は port の引数追加に合わせて
  `**_` を受けるようにした（アサーションは変えていない）。

## 2. `/episodes/*` `/research/*` の opt-in 認証（tests/unit/test_episodes_api_auth.py、ADR-0045 §4）

route は `episodes.router` と `research.router` の route 表から列挙して全件を試す（route を足して依存を
付け忘れると落ちる）。
- `required_every_route_without_a_token_is_401` / `required_a_wrong_token_is_401`: ON のとき無資格は handler に届かない。
- `required_a_read_only_token_cannot_post_but_passes_get`: GET は `read`、POST は `resume` を要求し、
  403 の形は介入 API の `missing_scope` と同じ。
- `required_a_resume_token_passes_authentication_on_every_route` / `..._really_reaches_the_handler`:
  正しい token は拒否されず、実際に handler（作成・参照）まで届く。
- `required_healthz_stays_open`: 死活監視は認証の外。
- `required_without_configured_principals_fails_closed_503`: ON で principal 未設定なら開けない。
- `required_reads_the_same_intervention_principals_setting`: 新しい認証方式を作っていない。
- `default_is_off_and_needs_no_token` / `off_ignores_even_an_invalid_token`: 既定 OFF は従来と同一
  （存在しない Episode は 401 ではなく 404）。

## 3. `/ui/` の静的配信（tests/unit/test_ui_hosting.py、ADR-0045 §1）

静的ディレクトリは dependency で差し替え、実 UI の資産に依存しない。
- `on_serves_index_files_...`: index・サブディレクトリの index・通常ファイルを配信し、CSP 等の 4 ヘッダが付く。
- `on_unknown_file_is_404_...` / `on_with_a_missing_static_directory_is_404_not_500`: 無いものは 404（UI 未配置でも 500 にしない）。
- `off_every_ui_path_is_the_same_404_feature_disabled_as_the_api` / `off_does_not_touch_the_files`:
  OFF の拒否は application route と同じ body で、存在しない directory でも変わらない（ファイルより先）。
- `path_traversal_never_leaves_the_static_directory`: 生の `..`・`%2e%2e`・`%2f`・`\\`・絶対パス・NUL が外のファイルを返さない。
  HTTP client は `..` を畳むため、`unnormalised_dot_dot_*` は ASGI に畳まれない path を直接渡す。
- `a_symlink_pointing_outside_is_not_served`: 実パスで検査する（symlink での脱出）。
- `ui_is_get_only`: 書き込みの口にしない。
