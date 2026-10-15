# 介入 API・CLI（JP-MAS-001 担当 C）のテストの理由

設計 [JP-MAS-001 v2.0](../design/JP-MAS-001-v2.0.md) §10、契約 [docs/domain/intervention.md](../domain/intervention.md) §4/§8、
[ADR-0041](../decisions/0041-decision-records-and-intervention.md) §8、不変条件 INV-47。HTTP の形は
[docs/operations/intervention-api.md](../operations/intervention-api.md)。

守る事故は2系統:

1. **既定で開く**: API は `0.0.0.0:8000` に bind しており、認証が無い（ADR-0041 Context の実査）。介入 API を足した
   だけで、ネットワーク上の誰でも hold・適用・mode 変更ができる状態を作ってしまう。
2. **AI が人の権限を持つ**: AI アシスタント・agent が mode を変える、予算・Schedule・公開設定に触れる（設計 §10、INV-47）。

全て unit（FastAPI のアプリ・設定の解析・CLI を fake と SQLite で動かす）。実 Temporal・実 MinIO・有料 API には
到達しない（INV-18）。B・A の実モジュールとの接続（`apps/api/intervention_backend.py::load_real_modules`）は
統合後に親が検査する（この文書の末尾）。

## principal 設定（`tests/unit/test_intervention_auth.py`）

| テスト | 何の失敗を捕まえるか |
|---|---|
| `test_token_hash_is_plain_sha256_hex` | 運用手順（`scripts/intervene.py hash-token` / `sha256sum`）で作った hash と照合方法がずれる |
| `test_authenticate_maps_a_token_to_its_principal` | token → actor・scope の対応が崩れる（別人として記録される） |
| `test_unknown_or_empty_token_is_not_authenticated` | 空 token・**hash そのもの**を token として出すと通る（設定ファイルが漏れたら即なりすまし） |
| `test_ai_principals_cannot_be_granted_human_only_scopes` | INV-47: AI（`ai_assistant` / `agent`）に `set_mode` を与えた設定が読み込めてしまう。`HUMAN_ONLY_SCOPES` から引くので、契約に人専用 scope が増えたら自動で対象になる |
| `test_system_actor_cannot_be_an_api_principal` | `system`（worker・Schedule の主体）を名乗る外部 principal で、人・AI の区別が記録から消える |
| `test_malformed_configuration_is_rejected` | 平文 token を key に置いた・大文字 hex・未知 scope（`set_budget` 等）・余分な項目・空 actor_id を黙って受け入れる |
| `test_config_errors_never_echo_the_key_or_a_clear_token` | 設定の誤りの例外文（ログに出る）に平文 token・hash が載る |
| `test_load_returns_none_when_unconfigured` / `test_settings_default_is_unconfigured` | 既定で介入 API が有効になる（事故 1） |
| `test_load_accepts_inline_json_or_a_file_path` / `test_load_rejects_a_missing_file` | 運用で使う2形式のどちらかが読めない・読めないファイルを「未設定」として黙って扱う |
| `test_settings_hide_the_principal_setting_in_dumps` | 設定値が `model_dump` / repr に出る（`SecretStr`） |
| `test_parsing_does_not_log_secrets` | 解析の経路が token・hash をログに書く |

unit で十分な理由: 解析は純粋関数で、外部 I/O はファイル読み込み1回だけ。

## HTTP API（`tests/unit/test_intervention_api.py`）

FastAPI のアプリ（`create_app`）を httpx の ASGI transport で叩く。store・SQLite（`conftest.session_factory`）は
**本物**、担当 A / B の関数（`receive_command`・read model・ReviewPacket・変更案ファイル）と workflow の start は
fake を `InterventionModules` で渡す。`receive_command` の fake は store の `insert_command` をそのまま呼ぶ最小の代役
（B の検証・HOLD_REQUESTED の書き込みは B のテストの範囲）。

### 既定で閉じる・認証（事故 1）
| テスト | 何の失敗を捕まえるか |
|---|---|
| `test_every_intervention_route_is_503_when_unconfigured` | 設定が無いのにどれかの route が開く。route の数と一致を検査するので、route を足して 503 の確認を忘れると落ちる。token があっても 503 |
| `test_a_misconfigured_principal_file_disables_the_api_without_leaking` | AI に set_mode を与えた設定で API が動く／理由が残らない／ログに hash・token が出る |
| `test_missing_or_unknown_token_is_401` | Bearer 以外の方式・空・未知の token が通る |
| `test_reads_require_the_read_scope` | 読み取りに scope が要らない（comment だけの principal で Episode が読める） |

### 読み取り
| テスト | 何の失敗を捕まえるか |
|---|---|
| `test_episode_view_comes_from_the_read_model` / `test_unknown_episode_is_404_for_reads` | read model を呼ばない・無い Episode で read model を呼ぶ（500 になる） |
| `test_decisions_are_read_from_the_store` | 判断記録の主体（agent / human）が応答から落ちる |
| `test_compare_revisions_passes_both_ids_to_the_read_model` / `test_compare_rejects_a_revision_of_another_episode` | 別 Episode の revision を URL の Episode 名義で比較できる（権限の境界の漏れ） |
| `test_review_packet_json_markdown_and_html` | 形式の取り違え・HTML 表示で packet の中身（LLM 出力を含む）がスクリプトとして実行される |
| `test_change_set_view_shows_diff_impact_and_cost` / `test_an_unestimated_cost_is_not_shown_as_zero` / `test_a_change_set_of_another_episode_is_404` | 見積りの無い変更を「0円」と見せて適用させる・別 Episode の ChangeSet が読める |

### command
| テスト | 何の失敗を捕まえるか |
|---|---|
| `test_a_new_command_is_202_received_and_started_after_commit` | 202 の前に commit していない／start しない／`received` を完了と表示／**AI の command を人の名義で記録**／B に渡す scope を広げる |
| `test_an_identical_resend_is_200_with_the_stored_status` | 同じ command_id の再送で2行目・2回目の適用を作る（INV-42 の API 側）／終端の command を start し直す |
| `test_reusing_a_command_id_with_another_payload_is_409` | 違う内容の再送で既存の command を上書きする |
| `test_missing_scope_is_403_and_nothing_is_persisted` / `test_command_for_an_unknown_episode_is_404` | 権限の無い command が保存される・無い Episode に command を作る |
| `test_invalid_envelope_or_payload_is_422_invalid_payload` | 形の違う command を B に渡す（B の中で 500 になる） |
| `test_command_status_distinguishes_received_from_applied` / `test_status_text_never_says_done_unless_applied` | 受付を「修正完了」と表示する（設計 §11・intervention.md §4）。全状態の文言を表で検査するので、状態を足して文言を忘れると落ちる |
| `test_a_failed_workflow_start_still_returns_202_and_logs_no_secret` | Temporal の一時的な不調で受付済み command を 5xx にする（クライアントが別 id で出し直して二重になる）／例外文言経由で secret がログに出る |
| `test_importing_a_change_proposal_file_creates_deterministic_commands` / `test_duplicate_proposals_in_one_file_are_distinct_commands` / `test_change_proposal_import_checks_scope_episode_and_shape` | 変更案ファイルの再取り込みで提案が重複する・並べ替えで別 command になる・同じ案2つが1つに潰れる・別 Episode のファイルを取り込む |

### INV-47
| テスト | 何の失敗を捕まえるか |
|---|---|
| `test_budget_schedule_and_visibility_commands_are_422_unsupported` | 予算・Schedule・公開設定らしい型が（人の token でも）受け付けられる。B に届く前に止まり、行が作られないことまで見る |
| `test_budget_schedule_visibility_fields_cannot_ride_on_other_commands` | 既存の型（set_mode・apply_change・resume・release_checkpoint）の payload に予算・cron・公開範囲を混ぜて通す |
| `test_the_router_exposes_no_budget_schedule_or_visibility_surface` | 介入 API に GET と2つの受付 POST 以外の書き込み（PUT/PATCH/DELETE や設定変更の route）が増える |
| `test_an_ai_principal_cannot_set_mode` | AI が mode を変える（人は通ることも見て、検査が「全部拒否」で空振りしていないことを確かめる） |
| `test_tokens_never_reach_the_logs` | 成功・401・403・422 のどの経路でも token・その hash がログ（record の属性を含む）に出る |

既存の `tests/architecture/test_api_no_heavy_work.py` が `apps/api/` 全体（新しい router・backend を含む）で
`execute_workflow`・`.result()`・`minio` の直接 import を禁じる（INV-16）。

## 失敗の写し方（`tests/unit/test_intervention_backend.py`）
store・SQLite は本物、A / B は fake。backend の例外が 500 や不明瞭なエラーにならず、機械可読な HTTP エラーになること。

| テスト | 何の失敗を捕まえるか |
|---|---|
| `test_unavailable_modules_are_503_and_store_only_routes_keep_working` | A / B の import 失敗が 500 になる／API 全体が止まる（store だけの route まで落ちる）／例外文言（パス・設定値）がレスポンスやログに出る |
| `test_unavailable_modules_are_retried_on_the_next_request` | 一度の失敗を永久に覚えて、A / B が揃っても 503 のまま |
| `test_a_receive_rejection_maps_to_a_machine_readable_error`（10 通り） | 受付の拒否が 500 になる／競合（stale・invalid_state）と形の誤りが同じ status でクライアントが再取得すべきか判断できない／拒否した行が残る／例外文に入力値が載る |
| `test_an_unknown_rejection_code_is_422_command_rejected` | B が理由を増やしたとき 500 になる／未知の文字列をそのまま返す |
| `test_a_rejection_in_an_import_rolls_back_every_proposal` | 変更案の取り込みが途中までしか入らない（全部か無し）。SQLite の SAVEPOINT が最外で commit する癖があるため、SAVEPOINT を使わない書き込みで見る |
| `test_a_read_model_without_the_episode_is_404` / `test_receive_not_found_from_b_is_404` | Episode が無いときの `None` を JSON `null` の 200 で返す／B の `not_found` が 422 になる |
| `test_comparing_incompatible_revisions_is_422` | 違う型の比較が 500／read model の例外文が返る |
| `test_resending_a_command_id_as_another_actor_is_409_without_naming_the_owner` | 他人の command_id を再送して、その command（他人の名義の行）を自分の受付として受け取る／409 の応答で保存済みの主体が分かる。本人の再送は 200 のまま |
| `test_a_workflow_start_timeout_still_returns_202_received` | Temporal が詰まったとき、受付済み command の応答が返らない／5xx になる（クライアントが別 id で出し直して二重になる）。未配送のまま残ること（再配送の対象）も見る |
| `test_the_temporal_client_is_reused_within_a_loop` | 要求ごとに接続して遅くなる |
| `test_a_failed_start_drops_the_cached_client` | 壊れた接続を使い回して以降の start が全て失敗する |
| `test_the_temporal_client_does_not_leak_across_event_loops` | 別の event loop で作った client を使って "attached to a different loop" で落ちる（テスト・再起動後） |

## 統合後に親が確かめること（fake でしか動かしていない部分）
`apps/api/intervention_backend.py::load_real_modules` は実 A / B との接続で、この branch では実物を import するテストが
無い（`ModulesUnavailableError` の経路だけ検査）。`TemporalCommandStarter` は Temporal を fake の `Client.connect` に差し替えて
検査し、`InterventionCommandWorkflow` の worker 実装は B2（`workers/intervention/`）にあり、同じ入力の形での start は `tests/unit/test_intervention_workflows.py` が time-skipping 環境で検査している。仮定は
[intervention-api.md §7](../operations/intervention-api.md)。

## CLI（`tests/unit/test_intervene_cli.py`）

`scripts/intervene.py` を path から読み込み、`main(argv, env=, transport=)` に fake transport を渡す（実ネットワークに
出ない）。1件だけ、CLI → ASGI（httpx）→ 実 router → store（SQLite）を通す。

| テスト | 何の失敗を捕まえるか |
|---|---|
| `test_the_cli_imports_only_the_standard_library` | CLI が DB・store を直接触るようになる（設計 §10「DB への直接書き込みより検証を通る API」）。標準ライブラリ以外の import を禁じる |
| `test_the_token_comes_only_from_the_environment` | token を引数で受ける（シェル履歴・`ps` に残る）／token 無しで送る |
| `test_show_reads_the_episode_view_with_a_bearer_token` / `test_the_base_url_can_come_from_the_environment` | URL の組み立て・認証ヘッダの誤り |
| `test_packet_writes_the_requested_format_to_a_file` | `md` を API の `markdown` に写し忘れる（422）・ファイルに書かない |
| `test_diff_shows_a_change_set_or_compares_two_revisions` / `test_status_reads_a_command` | 差分・費用確認と状態確認の経路の誤り |
| `test_comment_builds_an_envelope_with_a_fresh_uuid4_command_id` | command_id を使い回す（別の操作が 409 / 同一視される）・id を表示しない（再送できない） |
| `test_command_id_and_expected_revisions_can_be_given_for_a_safe_retry` | 再送用の id・期待版を envelope に載せない |
| `test_each_command_maps_to_its_envelope` / `test_propose_sends_the_payload_file_as_a_propose_change` / `test_import_posts_the_change_proposal_file_unchanged` | 各操作の型・payload の取り違え（例: apply の上限費用が落ちる） |
| `test_there_is_no_cli_command_for_budget_schedule_or_visibility` | INV-47 の CLI 側。予算・Schedule・公開設定・mode の操作を足す |
| `test_http_errors_exit_nonzero_and_print_the_detail` / `test_a_network_error_tells_how_to_retry_safely` | 409 等を成功扱いにする・応答なしで再送方法（同じ id）を示さない・例外文言から token が出る |
| `test_hash_token_prints_the_sha256_of_the_env_token` | 設定の key と CLI の token の hash 方法がずれる |
| `test_the_cli_round_trips_through_the_real_router` | CLI と router の形の食い違い（fake transport の検査だけでは見えない）。受付・状態・同一再送 200・違う内容 409 を通す |
