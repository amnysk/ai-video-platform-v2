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
