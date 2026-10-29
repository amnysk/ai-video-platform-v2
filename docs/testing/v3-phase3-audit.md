# v3 Phase 3 監査枝（claude/v3-p3-audit）のテストの理由

追加したテストごとに「何の事故・リスクを守るか」「どの不変条件を固定するか」「なぜそのレベルか」
「何を検出できるか」を残す（AGENTS.md / memory: test-design-rationale）。

## 1. integration テストの本番誤接続 guard（ADR-0079 / INV-121・122）

事故の型: `tests/integration/test_pipeline_schedule.py` が `localhost:7233` を直書きしていた。本番 Temporal /
PostgreSQL / MinIO は同じホストの同じ番号で公開されており、`avp-test` namespace・`avp_test` DB・
`artifacts-test` bucket も**本番サーバ上に実在する**。namespace / bucket / DB 名の guard はすべて通ったまま
本番サーバに書き込める。

### tests/unit/test_production_target_guard.py（INV-121）
純関数 `assert_isolated_targets` を、環境変数の辞書と注入した名前解決で検査する。単体にする理由: 本当にホストの
ポートへ繋ぐ試験は、それ自体が事故になる。ネットワークに触れず全分岐を網羅できる。
- `test_no_targets_configured_is_not_checked`: インフラ無しで unit だけ走る環境を壊さない（過剰拒否の防止）。
- `test_the_isolated_apptest_stack_passes` / `test_the_ci_ephemeral_services_pass`: 正規の2経路が通る。通らないと
  guard が常に落ちる実装（何も検査しないより悪い: 全員が迂回する）を検出する。
- `test_a_target_without_an_isolation_declaration_is_refused`: 宣言が無いまま `localhost:7233` / `:5432` / `:9000` /
  LAN IP を向けると例外。今回の事故そのもの。
- `test_a_mistyped_declaration_fails_closed`: 空・大文字小文字違い・末尾空白・`1`/`true` を拒否。環境変数の綴り違いで
  guard が外れる（fail open）実装を検出する。
- `test_the_ci_declaration_is_refused_outside_github_actions`: `ci-ephemeral` だけでは本番と同じホストで通らない。
- `test_the_apptest_declaration_never_allows_the_host_or_its_published_ports`: 隔離宣言を付けても、`localhost` や
  ホストの IP に解決される名前を向ければ拒否（宣言と接続先の不一致 = 宣言の流用）。
- `test_an_unknown_hostname_that_resolves_to_this_host_is_refused` / `test_ci_must_not_reach_a_non_loopback_host`: 名前で
  誤魔化してもホストに戻る経路、CI が外部ホストに繋ぐ経路。
- `test_the_production_compose_project_is_never_a_valid_declaration`: `apptest:avp2` は run-e2e.sh と同じく不可。
- `test_an_unparsable_target_fails_closed`: 解釈できない住所を「接続先なし」として素通りさせない。

### tests/unit/test_temporal_production_probe.py（INV-122）
偽の client で `assert_temporal_is_not_production` を検査する（本番へ繋がずに分岐を網羅するため単体）。
- `test_a_server_that_has_the_daily_schedule_is_production`: 日次 Schedule が在る = 本番。describe しか呼ばないこと
  （`described == [DAILY_SCHEDULE_ID]`）も断言し、検出器が副作用を持たないことを固定する。
- `test_a_server_without_the_schedule_passes` / `test_an_undecidable_answer_fails_closed` / `test_an_unreachable_...`:
  NOT_FOUND だけが「本番ではない」。権限エラーや UNAVAILABLE で判定できないときは拒否（fail closed）。
  到達不能は何もしない（各テストの skip に任せる）。
- 実機の検証（2026-10-29、読み取りのみ）: 本番 `localhost:7233` に対し、宣言なし = 静的に拒否、
  `ci-ephemeral`+`GITHUB_ACTIONS=true` を偽装 = 静的は通過し**動的検査が本番 Schedule を検出して拒否**、
  `apptest:avp2-oslog-c` + `localhost` = 静的に拒否。隔離スタック（project avp2-oslog-c-p3a）では
  `test_pipeline_schedule.py` / `test_minio_store.py` / `test_schedule_guard_temporal.py` が 5 passed / 0 skipped。

### tests/architecture/test_integration_no_hardcoded_addresses.py（INV-122）
- `test_no_hardcoded_connection_address`（ファイルごと）: `localhost:7233` 等・`TEMPORAL_ADDRESS = "..."`・
  `connect_test_client("...")`・DB URL の host の直書きを静的に検出。architecture レベルにする理由: 将来の新しい
  integration テストが同じ直書きを足すのは、実行しなくてもソースの走査で止められる。
- `test_the_detector_catches_the_original_incident`: 検出器が今回の一行（`TEMPORAL_ADDRESS = "localhost:7233"`）を
  実際に捕まえる（何も検出しない正規表現で緑になる事故を防ぐ）。内部名 `temporal:7233` は捕まえない。
- `test_integration_conftest_asserts_isolation_before_anything_else`: guard が他の guard より前（収集時）にある。

## 2. 承認の種別（ADR-0080 §A / INV-123）

事故の型: Phase 2 のデプロイで、所有者の条件付き指示を根拠にエージェントが release 承認ファイルを書いた。記録上は
所有者本人の承認と区別できない（`approver` が自由文字列）。同型の記録が breaker・予約の手動確定・保留解放にもある。

### tests/unit/test_approval_kind.py
純粋関数（`domain/approval.py`）なので単体。DB・ファイルに触れずに全分岐を網羅できる。
- `test_an_owner_reference_is_owner_kind` / `test_a_delegated_reference_names_the_owner_instruction_it_relies_on`: 種別が
  接頭辞から決まり、委任は委任元の所有者の指示を返す。
- `test_anything_that_does_not_state_a_valid_kind_is_not_an_approval`: 空・種別なし・実在しない日付・短い id・小文字・
  委任元なし・委任元がエージェント・所有者承認に委任元を付ける・二重の委任元・末尾の改行を拒否。「種別を書かない記録は
  所有者承認ではない」を fail closed で固定する。
- `test_the_owner_only_gates_are_the_ones_that_spend_or_publish`: 所有者専用の関門の集合を固定（黙って減らす・
  委任可へ移すのを検出）。`test_an_agent_delegated_approval_never_satisfies_an_owner_gate`: 全関門で委任は拒否、
  所有者本人は通る。`test_a_missing_reference_never_satisfies_an_owner_gate`: 参照なしも拒否。
- `test_the_stored_actor_states_its_kind_and_a_legacy_value_does_not` / `test_an_actor_name_cannot_forge_a_kind`:
  DB を変えずに既存列へ `<kind>:<name>` で保存する方式の読み書き。旧い値（`owner` など）が所有者承認と読まれない
  こと、名前に `owner:` を仕込んで偽装できないこと。

### tests/unit/test_release_approval_kind.py
実際の判定点（release preflight・CLI の検証）に規則が結線されていることを固定する。結線の抜け（規則だけ在って
どこも使わない）を検出するため、純関数テストとは別に置く。
- `test_an_owner_approval_passes_and_says_so` / `test_a_delegated_approval_passes_a_flags_off_deploy_...`: Phase 2 の
  正当な委任デプロイは通り、結果に `approver_kind` / `delegated_by` が残る（後から監査で区別できる）。
- `test_a_record_that_does_not_state_its_kind_...` / `test_a_record_without_any_approval_ref_key_is_unknown`:
  旧書式の承認ファイルは UNKNOWN（デプロイを止める）。
- `test_an_explicit_kind_that_contradicts_the_reference_is_rejected`: 種別を二重に書いて食い違う記録を拒否。
- `test_a_delegated_approval_cannot_enable_an_owner_gate`（5 関門）/ `test_an_unknown_gate_name_in_enables_...`:
  委任で所有者専用の関門を開く宣言は BLOCKED、未知の名前は UNKNOWN。
- `test_the_release_reservation_waiver_stays_owner_only` / `test_provider_credit_actions_accept_only_the_owners_own_reference`
  / `test_manual_reservation_resolution_accepts_only_the_owners_own_reference`: waiver・provider 資金操作・予約の手動確定が
  委任を拒否し、保存される actor が `owner:<name>`、`reconciled_by` の既存の形（`operator:<name>`）は変わらない。
- 既存テストの変更 2 点（仕様変更が ADR-0080 で先に承認済み。理由はコミットメッセージにも書く）:
  `tests/unit/test_release_preflight.py::_approval` に `approval_ref` を足した（承認記録の必須項目が増えたため）、
  `tests/unit/test_reconcile_reservations_script.py::test_audit_row_never_stores_the_raw_token` の `row.actor` の期待を
  `me` → `owner:me`（actor 列が種別つきになったため）。

## 3. API の認証（ADR-0080 §B / INV-124・125）

事故の型（監査 2026-10-29）: 全ての変更系ルートが無認証で `0.0.0.0:8000`（LAN）に公開。`/upload` は flag OFF の間
品質ゲートを通らず、保留行のない古い `blocked` Episode（本番 15 件）は `/resume` で再開できる。

### tests/unit/test_api_auth.py（`real_api_auth`: 本物の認証を使う）
API テスト（ASGI 直結）にする理由: 認証は HTTP の入口の性質で、DB・workflow は偽物でよい。起動されたか（副作用）を
偽の starter が **呼び出し記録**で断言する（拒否のつもりで handler まで届いていた、を検出する）。
- `test_there_are_mutating_routes_to_protect` と全ルートを回すテスト: ルートは **OpenAPI から列挙**する。FastAPI の
  `app.routes` は include した router を `_IncludedRouter` に包むので、`APIRoute` を走査する素朴な実装は 0 件で
  全部通ってしまう（実装中にそれが起きた）。0 件で緑にならないよう件数も断言する。
- `test_every_mutating_route_refuses_when_no_token_is_configured`: 未設定は 503（fail closed）。
- `test_every_mutating_route_refuses_a_missing_or_wrong_credential`（8 種）: ヘッダ無し・`Bearer` のみ・`Basic`・
  スキーム無し・1 文字違い・末尾空白・小文字スキーム。401 と `WWW-Authenticate`。
- `test_a_valid_token_reaches_the_handler` / `test_read_only_routes_stay_open`: 正常系と、GET が開いたままであること
  （過剰な閉鎖で監視・運用が止まるのを防ぐ）。
- トークンファイル: `test_a_missing_or_malformed_token_file_...`（空・短い・未知の actor・同一 token の二重利用・形式違い）、
  `test_an_unset_or_absent_path_...`、`test_a_token_file_readable_by_others_is_refused`（0644 / 0660 / 0604）。
- `test_comparison_is_constant_time_and_checks_every_entry`: `compare_digest` を差し替えて、先頭一致でも全件を比較する
  ことを固定（時間差で順序・長さを漏らす実装の検出）。
- `test_the_credential_never_appears_in_a_refusal` / `test_every_authenticated_mutation_logs_its_actor_...`: token が
  応答・ログに出ない。actor は残る。
- INV-125: `test_manual_upload_cannot_bypass_a_block_with_any_token` / `test_review_is_approved_by_the_owner_token_...`
  / `test_a_passing_report_lets_the_agent_token_upload`。既存の `test_quality_gate_wiring.py` の手動投稿テストは
  所有者として走り続ける（挙動不変）。
- `test_every_operator_script_that_posts_to_the_api_sends_a_token` / `test_the_resume_client_sends_the_token_...`:
  認証なしで API を叩く運用スクリプトを残さない（呼び出し元の更新漏れ）。件数の下限も断言して走査の空振りを防ぐ。

### tests/conftest.py（autouse）
既存の API テスト 10 ファイルは認証ではなく各ルートの挙動を試す。`create_app()` の戻り値の
`dependency_overrides` で認証の依存だけを「所有者」に上書きする（本番コードに迂回は無い）。`real_api_auth` を付けた
テストだけ本物の認証を使う。既存テストの本体は変更していない。
