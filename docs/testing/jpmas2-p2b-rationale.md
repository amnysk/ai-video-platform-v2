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

## 4. UI 向け操作カタログ（tests/contract/test_operations_catalog_sync.py、ADR-0045 §2）

- `committed_catalog_equals_a_fresh_generation`: `apps/web/static/operations.json` は `OPERATIONS` から
  `scripts/export-operations.py` で作ったものと同じ（OpenAPI 断片と同じ方式。手で直した JSON は落ちる）。
- `the_13_names_match_operations_exactly`: UI だけの操作を足していない・操作を落としていない（13 のまま）。
- `each_entry_carries_the_catalog_facts_and_nothing_invented`: method・path・scope・人限定・effect はカタログのまま、
  キー集合も固定（カタログに無い「費用が発生しうるか」等を推測して足さない）。
- `fields_come_from_the_request_models`: 必須/任意/null 可/enum が pydantic model 由来で、path parameter が path から取れる。
- `catalog_version_is_stable_and_changes_with_the_content`: 版は決定的で、内容が変われば変わる。

## 5. command の reason / correlation_id（tests/unit/test_intervention_read_model.py）

- `test_command_view_exposes_reason_and_correlation_id_but_never_the_payload`: UI の監査が「未記録」だらけに
  ならないよう `CommandView` に 2 項目を足した。`reason` は `payload.reason` が **str のときだけ**
  （None・数値・reason の無い型は None）、1000 文字で切り詰め、payload の他のキーは JSON に現れない
  （全文露出の回帰を防ぐ）。
- `tests/web/timeline.test.mjs`: 値があれば表示、無ければ `未記録`（捏造しない）。

## 6. 運用者 UI のテスト（旧 docs/ui/README.md から移動、ADR-0045）

| テスト | 守るもの |
|---|---|
| `tests/web/errors.test.mjs` | `OperationErrorCode` / `RejectReason` の全値に日本語の理由があること（新しい code が黙って英語のままにならない） |
| `tests/web/status.test.mjs` | ラベル表が `EpisodeStatus` 等の enum と過不足なく一致、要注意が enum の部分集合 |
| `tests/web/cursor.test.mjs` | 複合 cursor と重複排除（ページ境界で行が重複/消失しない） |
| `tests/web/stages.test.mjs` | 振り分け表のキーが実在する enum、未分類を捨てない、欠落を補完しない |
| `tests/web/timeline.test.mjs` | 監査の併合順、payload 全文を出さない、trace id の捏造をしない |
| `tests/web/requests.test.mjs` | body のキーが実物 `operations.json` の `body_fields` と一致、適用前検査（見積り null で送らない） |
| `tests/web/api.test.mjs` | Bearer/相関 ID、401 でトークン破棄、ETag は返ったときだけ、トークンは sessionStorage のみ |
| `tests/web/poller.test.mjs` | 前面のときだけ polling、stop 後は何もしない |
| `tests/contract/test_web_static_csp.py` | CSP を破る記述（inline、`on*`、外部 URL、innerHTML、localStorage、console）が入らない |
| `tests/unit/test_web_node_tests.py` | 上記 node テストを pytest から実行（node が無ければ skip） |

`tests/web` は実物の `apps/web/static/operations.json` を直接読む（コピーは持たない）。

- `tests/web` は backend が生成する実物 `apps/web/static/operations.json` を直接読む（コピーを持たない。
  推測した形のコピーが実物とずれて、frontend が `request_fields`/`incurs_cost` を前提に書かれていたため）。
  `tests/web/operations.test.mjs` は、UI が有料かどうかを `operations.json` の `paid` だけから決めること（JS に
  操作名のリテラルを持たない。フラグを書き換えると判定が変わる）を固定する。有料の集合そのもの
  （`start_agentic_production` / `resume_production` / `apply_approved_change`）は
  `tests/contract/test_operations_catalog_sync.py::test_the_paid_operation_set_is_pinned` が固定し、変えるなら意図的に落ちる。
  `tests/e2e_ui/interventions.test.mjs` 9 は再開ダイアログの警告・確認チェック・権限ヒントの「有料」を実ブラウザで確かめる。
- `tests/web/render_gate.test.mjs`（fake timer）: 遅い render が後から終わっても新しい画面を上書きせず、poller が 1 本（離れたら 0 本）であること。
- `tests/unit/test_ui_hosting.py` の P2-6 群: flag OFF の 404・405（POST/HEAD ほか）・500 でもセキュリティヘッダが付き、
  `/ui` 以外には付かない（route 内で付けていた頃は dependency の拒否と 405 で漏れていた）。
- `tests/unit/test_ui_hosting.py::test_on_ui_without_trailing_slash_redirects_to_ui_slash`: `/ui` のままだと
  `./js/main.js` が `/js/main.js` に解決されて画面が白くなる。`/ui/` へ 308。OFF のときは redirect せず
  `404 feature_disabled`（OFF の挙動を漏らさない）。

## 7. ブラウザ E2E（tests/e2e_ui/、ADR-0045）

node の単体テスト（§6）は DOM・CSP・実サーバーの応答を通らない。画面の約束は実ブラウザで確かめる。

- **構成**: `tests/e2e_ui/server.py` が本物の FastAPI app を uvicorn（別 thread・ephemeral port）で起動する。
  `ApplicationService` は本物（scope・人限定の検査も本物）で、port だけ `tests/support/ui_e2e_world.py` の
  メモリ実装（command_id の冪等・再利用 409・stale revision 409 を backend と同じ規則で再現）。
  DB・Temporal・MinIO・有料 API には接続しない。`/__e2e/*` はテスト専用の管理口で、本番 app には無い。
  ブラウザは `playwright-core`（`cd tests/e2e_ui && npm ci`。lockfile 固定、`node_modules` は git 管理外）と
  システムの Chrome。どれかが無ければ pytest wrapper（`test_ui_e2e.py`）と各 spec が skip する。
- **サーバーが権威であること**: 全リクエストを ASGI の外側で記録し（principal・status・error・command_id）、
  「ボタンを隠した」ではなく「サーバーが受けて拒否した」ことを assert する（シナリオ 4・5・6・7）。
- `list_auth.test.mjs`（1, 2）: 129 件（125 件が同一 `created_at`）を辿って重複も欠落も無いこと、2 ページ目以降が
  `before_id` を送ること。401 でトークンを破棄、保存先は sessionStorage だけ（localStorage・cookie・URL・DOM に無い）。
- `detail.test.mjs`（3, 4）: 記録の無い項目が「未記録」、LLM 費用が 0 円でなく未計測。監査の `reason` /
  `correlation_id` が read model の値（ブラウザが送った `X-Correlation-ID` と一致）で、無ければ「未記録」。
  読み取り専用 principal の hold / apply が 403 `missing_scope` で、command が 1 件も受け付けられていないこと。
- `interventions.test.mjs`（5–8）: 二重クリックで POST 1 回。応答だけ失われた通信失敗の再試行は同じ `command_id`
  （サーバーは 200 の再送扱い・command は 1 件）、403 のあとは新しい `command_id`。`stale_control_revision` で
  説明・workspace 再読込・再送不可。適用は上限額の空/不足/未確認を送らず、見積り null は画面もサーバー
  （422 `cost_not_estimated`）も拒否。別 Episode へ移動するとダイアログが閉じ、閉じたダイアログの送信ボタンを
  保持して押しても送られない（このテストが `dialog.js` の穴を見つけて直した）。
- `platform.test.mjs`（9–11）: flag OFF は `/ui/*`・`/v1/*` とも 404 `feature_disabled` で資産を返さない。全画面・
  全ダイアログで CSP 違反 0 件と console の error/warning 0 件、4 つのセキュリティヘッダ、対照として inline
  script が実際に遮断されること（テストが空振りしていない証拠）。polling は progress だけを読み、タブが隠れる/
  画面を離れると止まる（`page.clock` で時間を進めて件数を数える）。`/ui` は `/ui/` へ redirect。
- 見つけた問題: `<link rel="icon" href="data:,">` は CSP（`default-src 'self'` が img-src に効く）違反で
  console error になる。`./favicon.svg`（同一 origin）に替えた。
- 注意: ページは `unsafe-eval` が無いため Playwright の `waitForFunction` は使えない（`harness.mjs` の `until`）。
