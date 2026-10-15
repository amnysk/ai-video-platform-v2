# JP-MAS-002 P0-B（application 層と ProposeStoryboardChange）のテストの理由（ADR-0043 §1）

テストを足した理由を、足したのと同じコミットで残す。

## 1. レイヤ（tests/architecture/test_layering.py ほか）

- `test_application_layer_is_shipped_and_isolated`: `application/` が worker/api image（Dockerfile の COPY）と
  package 探索（pyproject）に入っていること、`infrastructure` を import できない表であること（INV-6）。
  image に入っていないと API が起動時に落ちる。
- 既存の architecture テストの対象 tuple に `application` を足した（logging / storage / live-call / piper /
  schedule guard / pipeline scheduling）。新しい層が各禁止規則の検査から漏れないようにするため。
  緩和ではなく対象の追加。
