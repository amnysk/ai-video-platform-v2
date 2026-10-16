# JP-MAS-002 P2-A（HTTP adapter・観測・CORS）のテストの理由（ADR-0044 §4/§5）

テストを足した理由を、足したのと同じコミットで残す。service 本体（`application/service.py`）のテストは
別の変更が持つ。ここは「HTTP が catalog と一致し、service を呼ぶだけであること」を固定する。

## 1. 契約（tests/contract/test_application_operations_http.py、`OPERATIONS` を全件 parametrize）

- `test_every_operation_has_exactly_one_route`: 13 操作それぞれに `/v1/intervention` + `http_path` の route が
  ちょうど 1 つ（宣言したのに route が無い・二重登録、を防ぐ）。
- `test_the_request_body_schema_is_the_request_model` / `test_the_success_response_schema_is_the_result_model`:
  route の OpenAPI schema が catalog の model と同じ（INV-52。TypeScript client の生成元がずれない）。
- `test_the_route_forwards_caller_path_and_body_and_returns_the_result_unchanged`: route は認証済み principal →
  `Caller`（相関 ID つき）、path・body・query をそのまま service の同名メソッドへ渡し、戻り値を変えない。
  business logic が route に混ざると落ちる。
- `test_no_principal_is_401_and_unconfigured_is_503`: 全 route が既存の介入 API と同じ認証（無資格は service に届かない）。
- `test_flag_off_is_404_feature_disabled_before_anything_else`: `JPMAS2_APPLICATION_API=false` では認証より先に 404。
- `test_status_is_created_or_accepted_when_new_and_200_on_resend`: draft 201、command 系 202、再送 200（既存の
  proposals と同じ規則を全 POST に）。
- `test_an_operation_error_maps_to_the_same_status_and_code_on_every_route`: `ERROR_STATUS` の写像が全 route で
  同じ（新設 `change_set_not_found`=404・`invalid_state`=409 を含む）。code は detail にそのまま出る。
- `test_a_missing_correlation_header_gets_a_generated_id`: 相関 ID は無ければ uuid4、不正な値（改行等）は捨てる
  （ログ注入を防ぐ。既存 middleware と同じ許容文字）。
- `test_list_episodes_takes_limit_and_before_as_query_and_validates_them`: query の既定・上限が `ListEpisodesRequest` 由来。
- `test_drafts_is_not_parsed_as_an_episode_id_and_existing_routes_are_untouched` /
  `test_the_catalog_paths_do_not_collide_with_existing_intervention_routes`: `POST /episodes/drafts`・
  `GET /episodes` が既存の `/episodes/{episode_id}` 系と衝突しない。

## 2. 既存テストの変更（tests/contract/test_application_contract_sync.py）

- `test_the_fragment_has_only_the_application_paths_and_the_referenced_schemas`: 「path が提案の 1 本だけ」という
  検査を「catalog の 13 path の全部」に変えた。仕様変更が ADR-0044 §4 として先に承認されている。
  `CommandView` を含まない検査（介入 API の型を混ぜない）は残した。他の検査は変更なし。

## 3. 観測（tests/unit/test_application_http_observer.py）

- 1 操作 = `application.operation.completed` 1 行、ADR §5 の項目が入る。`succeeded`/`rejected`/`failed` の写像と
  level（failed だけ ERROR）。未取得の値は付けない。ログの故障は呼び出し元へ伝えない（INV-38）。

## 4. CORS（tests/unit/test_application_http_cors.py）

- 既定（空・空白だけ）は middleware を付けない。設定した origin だけ、GET/POST と 3 つの header（Authorization・
  Content-Type・X-Correlation-ID）を許し、credentials は許さない。`*` は起動時に拒否（token は header で渡すので
  全許可にする理由が無い）。

## 5. MCP との共通化（tests/unit/test_application_http_mcp_parity.py）

- `OperationName` で service のメソッドを引くだけの test 専用 adapter と HTTP route が、同じ結果・同じ error code を
  返す。MCP server は作らない（P3）。「同じ service を別の入口が呼べる」という ADR-0044 §2 の要件の証拠。

## 設定キー `API_CORS_ORIGINS`（AGENTS.md §8 の grep）

`grep -rn "api_cors_origins\|API_CORS_ORIGINS" apps/ workers/ domain/ infrastructure/ contracts/ docs/`（追加前）:
ヒット 1 件（`docs/decisions/0044-…md`: 設計書。読み手／書き手のコードは 0 件、無関係 0 件）。
追加後の読み手は `apps/api/main.py:create_app`（`infrastructure.config.cors_origins` 経由）の 1 か所、書き手は環境変数のみ。
