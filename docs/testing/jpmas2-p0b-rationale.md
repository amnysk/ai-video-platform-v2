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
