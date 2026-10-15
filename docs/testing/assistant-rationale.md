# 対話 AI アシスタント adapter のテストの理由（JP-MAS-001 / ADR-0042）

テストを足した理由を、足したのと同じコミットで残す。

## 1. `intervention.api.request`（tests/unit/test_assistant_request_log.py）

- `test_a_request_is_logged_with_route_actor_status_and_correlation`: 「どの AI セッションのどの tool 呼び出しが
  どの変更を起こしたか」を OpenSearch 側から辿る相関（ADR-0042 §6）。route は実 path でなく template
  （episode_id が cardinality を増やさない）、主体は認証が成功した principal の actor_type／actor_id。
- `test_neither_token_nor_authorization_nor_body_is_logged`: INV-39。介入 API は本文（コメント）と
  Bearer token を扱う。middleware がそれらを読まないことを、実際の JSON 整形器の出力と caplog の両方で見る。
- `test_a_rejected_request_is_logged_without_an_actor`: 401 でも要求は記録する（不正アクセスの痕跡）が、
  誰かを推測して書かない・誤った token を写さない。
- `test_unsafe_header_values_are_not_logged`: 相関 ID・tool 名はクライアント入力。改行・空白を含む値を
  そのままログへ入れるとログ注入になるので、安全な文字だけのときに限る。
- `test_only_the_intervention_prefix_is_logged_as_intervention`: 他の route（health 等）の既存ログを変えない。

## 2. 判断の説明と横断検索（tests/unit/test_assistant_insights.py）

- `test_explanation_is_built_from_stored_rows` / `test_interventions_target_only_the_decisions_revisions`:
  説明が保存済みの行（decision・agent_run・注記・command・revision_links）から組み立てられ、別の版への
  コメントを混ぜないこと。
- `test_reported_rationale_is_labelled_self_reported_not_verified`: モデルの自己申告を検証済みの事実として
  表示しない（ADR-0042 §5、設計 §7）。`rationale_kind` を言い換えない。
- `test_missing_data_is_reported_as_not_recorded_never_filled` /
  `test_a_decision_without_rationale_or_agent_run_lists_what_is_missing`: 欠測を補完しない。token・費用が NULL
  のとき 0 ではなく null と欠測一覧で返す（ADR-0041 の負債の可視化）。
- `test_recorded_token_usage_and_cost_are_shown_as_recorded`: 上の裏。記録があれば欠測にしない。
- `test_unknown_or_foreign_decision_is_404`: 他 Episode の判断を別 Episode の経路で読ませない。
- `test_explanation_requires_read_scope` / `test_search_requires_read_scope`: 新 route も介入 API の認証を通る。
- `test_search_filters_by_type_role_and_episode`: 横断検索の条件（役割は agent_runs 経由）。不正な役割は 422。
- `test_search_paginates_with_a_keyset_cursor_and_caps_the_limit`: 欠落・重複のないページング、limit の上限、
  不正な cursor の拒否。
- `test_the_new_routes_are_read_only_gets`: INV-47 の「書き込みは command の受付だけ」を新 route も守る。

## 3. tool 定義の契約と境界（tests/contract/test_assistant_tools_contract.py、tests/architecture/test_assistant_boundary.py）

- `test_the_catalogue_has_exactly_the_agreed_tools` / `test_every_input_schema_is_a_valid_object_schema`:
  LLM に見せる定義が合意した15個で、全て正しい JSON Schema（`additionalProperties: false`）であること。
- `test_command_tools_derive_their_scope_from_the_single_source` / `test_effects_are_what_the_adr_says`:
  必要 scope を literal で書き直すと `REQUIRED_SCOPE` と食い違う（AGENTS.md §8）。効果の種別は ADR-0042 §2 どおり。
- `test_no_tool_changes_budget_schedule_or_visibility`: INV-47 の検査を tool の表面にも掛ける。使う command 型は
  comment / request_hold / propose_change / apply_change / resume だけ。
- `test_proposal_schemas_do_not_invent_payload_fields` /
  `test_a_schema_valid_proposal_is_accepted_by_the_real_payload_model`: API が受け付けない項目を tool が
  約束すると、LLM が必ず 422 を受ける。schema の項目を payload model の項目に縛り、schema に従った入力から
  作った payload が `extra="forbid"` の本物の model を通ることを確かめる。台本の映像文 `visual` は
  サーバが拒否するので露出しない。
- `test_command_id_is_deterministic_per_tool_call`: INV-49 の核。namespace の値を固定して検査する
  （変えると既存の再試行の冪等性が切れる）。
- `test_the_catalogue_has_one_declaration_site`: tool 名を別の module に書き直さない。
- INV-48（test_assistant_boundary.py）: adapter が DB・Temporal・infrastructure を import すると、scope と
  AI の有料適用拒否を迂回できる。関数内の遅延 import も `ast.walk` で拾い、検査自体も別のソースで確かめる。

## 4. adapter の振る舞い（tests/unit/test_assistant_adapter.py）

- 相関ヘッダ・token: 全ての要求に `X-Correlation-ID` / `X-Assistant-Tool` を付けること、token が結果・例外・
  repr に出ないこと（transport 障害の文言にも入れない）。
- 入力の検査: 余分な項目・必須の不足・path に入る不正な値（`../`）を、要求を送る前に弾く。
- 読み取りの route 対応、未見積り費用の `null` 保持、API エラーを例外にせず返すこと。
- command: 同じ tool 呼び出しは同じ `command_id`、違えば違う（INV-49）。再送は `replayed`。権限不足は
  `missing_scope`（apply 以外で人の手順を作らない）。応答なしは同じ `tool_call_id` での再試行を促す。
- `apply_change`: 提示した費用と一致したときだけ1回送る / 違えば**送らず** `cost_changed`（`POST` の記録で確認）/
  `null` と `0` を同一視しない（5通りの組）/ 未見積りは明示的な `null` でだけ適用 / proposed 以外は適用しない /
  `403 missing_scope` も AI の有料適用の拒否（command が rejected・forbidden）も `needs_human_approval` で、
  再試行しない / 再試行は保存済みの command を返し費用を再検査しない（適用後に変更案が applied になるため）。

## 5. CLI（tests/unit/test_assistant_cli.py）

- `tools` の出力が定義と一致すること、`--tool-use` が最小の形であること。
- 環境変数名が `scripts/intervene.py` と同じであること（`test_the_env_names_are_the_same_as_intervene_py`）。
  別の名前にすると、同じ token を2つの CLI に渡せない。
- token を stdout / stderr に出さないこと。終了コードの区別（使い方 2 / 拒否 1 / 応答なし 3）。

## 6. 本物の API との結合（tests/unit/test_assistant_e2e.py）

- MockTransport は API の実際の挙動（202 と 200 の区別、command_id の再送、`extra="forbid"`）を保証しない。
  INV-49 の「サーバが既存の command を返す」を、本物の FastAPI アプリ・本物の store（SQLite）で確かめる。
- `test_a_retried_tool_call_is_one_logical_change`: 2回呼んで DB の command が1行、`202`→`200`、actor が
  `ai_assistant`、相関 ID が command に保存される。
- `test_apply_with_a_stale_cost_sends_no_command_to_the_real_api`: 古い費用では command が0行のまま。
- `test_the_server_logs_the_assistant_correlation_and_never_the_token`: ログの相関と token の非出力。
- 担当 B の `receive_command` は fake（統合後の検査は親が行う）。AI の有料適用拒否の実挙動は
  `tests/unit/test_intervention_changes.py` が、adapter 側の写像は test_assistant_adapter.py が持つ。
