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
