# JP-MAS-002 P0-B（application 層と ProposeStoryboardChange）のテストの理由（ADR-0043 §1）

テストを足した理由を、足したのと同じコミットで残す。

## 1. レイヤ（tests/architecture/test_layering.py ほか）

- `test_application_layer_is_shipped_and_isolated`: `application/` が worker/api image（Dockerfile の COPY）と
  package 探索（pyproject）に入っていること、`infrastructure` を import できない表であること（INV-6）。
  image に入っていないと API が起動時に落ちる。
- 既存の architecture テストの対象 tuple に `application` を足した（logging / storage / live-call / piper /
  schedule guard / pipeline scheduling）。新しい層が各禁止規則の検査から漏れないようにするため。
  緩和ではなく対象の追加。

## 2. 操作契約（tests/contract/test_application_contracts.py）

- `test_shared_error_codes_are_spelled_like_reject_reasons`: `OperationErrorCode` が既存 `RejectReason` と
  同じ綴りを使う（入口ごとに code が割れない。ADR-0043 §1）。
- `test_adr_error_vocabulary_is_exact`: ADR が列挙した語彙（再利用10 + 新設4）から勝手に増減しない。
- `test_visual_description_limit_is_the_override_limit`: 提案の長さ上限が `SceneOverrideVisual` と同じ値
  （上限を超える提案が受付で初めて落ちる、を防ぐ。定義は1箇所）。
- `test_edits_are_bounded_and_extra_fields_forbidden`: edit は 1〜12 件、未知の項目は拒否。
- `test_edit_command_ids_are_deterministic_per_request_and_index`: 再送が同じ command 群になる（INV-42/50）。

## 3. ユースケース（tests/unit/test_propose_storyboard_change.py）

- `test_one_propose_command_per_edit_with_a_validated_payload`: edit ごとに1つの propose_change、本物の
  payload model を通ること、期待版の素通し、作った command だけ start すること。
- `test_a_replay_starts_nothing`: 再送で workflow を二重に起動しない。
- `test_unsupported_edits_are_refused_before_the_port`: framing 等・visual_description 欠落は
  `unsupported_change`（`storyboard.patch_scenes` 未実装）で、port に何も渡さない。
- `test_duplicate_scenes_mismatched_episode_and_missing_scope`: scene 重複・Episode 不一致は invalid_payload、
  propose scope が無ければ missing_scope。いずれも port に届かない。

## 4. 縦断スライス（tests/unit/test_application_slice.py、INV-50）

本物の FastAPI・`receive_command`・`apply_command`・SQLite。fake は workflow の start だけ。

- `test_http_and_internal_port_give_the_same_result_shape_and_change_sets`: HTTP と内部 port（+ use case）が同じ
  結果型を返し、適用すると両方が `scene.override_visual` の ChangeSet になる（主体の種別だけが違う）。
- `test_resending_the_same_request_returns_the_existing_commands`: 再送は既存 command（created=false）、
  HTTP は 200、command も ChangeSet も増えない（INV-42）。
- `test_the_same_request_id_with_other_content_is_command_id_reused`: 同じ request id で別内容は両入口で
  `command_id_reused`（HTTP 409）。
- `test_unsupported_fields_give_unsupported_change_from_every_entry`: framing 等は両入口で同じ code。何も保存しない。
- `test_missing_propose_scope_is_missing_scope`: propose scope が無い主体は両入口で `missing_scope`。
- `test_the_internal_port_refuses_a_human_caller`: 内部 port は人・system として command を作らない。
- `test_the_flag_off_is_404_feature_disabled_before_anything_else`: 既定オフ。認証・本文検査より先に 404。
- `test_unknown_episode_and_episode_mismatch`: 存在しない Episode は 404、path と提案の Episode 不一致は 422。
- `test_a_stale_base_revision_ends_as_a_conflict_without_a_change_set`: 古い storyboard revision への提案は
  既存どおり適用で CONFLICT になり ChangeSet を作らない（新しい競合規則を作らない）。

## 5. アシスタント tool `propose_storyboard_patch`（同ファイル後半、tests/contract/test_assistant_tools_contract.py）

- `test_the_assistant_tool_gives_the_same_result_shape_and_change_sets`: tool が HTTP と同じ結果型を返し、
  request の command_id が `tool_command_id`（INV-49）、edit の command_id が `storyboard_edit_command_id`。
- `test_a_retried_tool_call_returns_the_existing_commands`: 同じ tool 呼び出しの再試行は既存 command、同じ
  tool_call_id で別内容は `command_id_reused`。
- `test_the_tool_reports_the_same_refusal_codes_as_the_other_entries`: unsupported_change / missing_scope /
  feature_disabled が他の入口と同じ code で、何も保存しない。
- `test_the_tool_passes_expected_control_revision_through`: 楽観的な競合検出の版が command まで届く。
- 既存 `test_the_catalogue_has_exactly_the_agreed_tools` / `test_expected_revision_fields_exist_on_the_envelope_...`
  の期待集合に `propose_storyboard_patch` を追加した（件数 15 -> 16）。ADR-0043 §1 が承認した新 tool で、
  「合意した tool の集合」を検査するテストなので集合の更新が仕様変更の反映になる（検査自体は緩めていない）。

## 6. CLI（tests/unit/test_intervene_propose_storyboard.py）

- `test_it_posts_the_proposal_to_the_application_route`: CLI が HTTP route にだけ送り（DB に触れない）、
  request の command_id・期待版・提案を本文に載せ、再送用の command_id を stderr に出す。
- `test_without_a_command_id_it_makes_one_and_omits_unset_options`: 省略時は uuid4、未指定の項目は送らない。

## 7. 契約同期（tests/contract/test_application_contract_sync.py、INV-52）

- `test_the_route_request_schema_is_the_request_model` / `test_the_route_response_schema_is_the_result_model`:
  OpenAPI の route が `contracts/application.py` の model と同じ形（`$ref` 解決後）。route の本文を手書きで
  ずらしても気づけるように。FastAPI は OpenAPI から `default: null` を落とすので、両側で落として比べる。
- `test_the_tool_input_schema_is_derived_from_the_proposal_model`: tool の入力 schema が提案 model の各項目と
  required に一致し、`command_id` を含まない（INV-49）。
- `test_the_committed_openapi_fragment_equals_a_fresh_generation`: 固定した `docs/api/openapi.application.json` が
  生成結果と同じ（UI の client 生成元が古いまま残らない）。
- `test_the_fragment_has_only_the_application_paths_and_the_referenced_schemas`: 断片に他の route の型を混ぜない。

## 8. 単一変更経路（tests/architecture/test_single_change_path.py、INV-50）

- `test_only_pass_through_modules_know_the_proposal_types`: 提案の型を知る module は use case・定義・schema 導出・
  HTTP の本文型だけ。他の入口が自前で解釈し始めたら落ちる。
- `test_only_the_use_case_turns_a_proposal_into_scene_overrides`: 提案の型と `SceneOverrideVisual` を同じ
  module で扱うのは use case だけ（変換の複製を防ぐ）。
- `test_only_the_use_case_derives_edit_command_ids`: edit ごとの command_id の導出が1か所（再送の冪等性の根拠）。
