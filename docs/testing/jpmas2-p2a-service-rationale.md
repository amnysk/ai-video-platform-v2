# JP-MAS-002 P2-A（Application Service・読み取り adapter）: テスト設計の理由

ADR-0044 §2 の `ApplicationService` と `EpisodeReadPort` の実装。権限・観測・冪等・費用の検査を
「1か所」に置いた変更なので、各テストは「その1か所が全13操作に効いている」ことを固定する。

## 1. ApplicationService（tests/unit/test_application_service.py、fake port）

- `test_every_operation_is_a_service_method_named_after_it` / `test_the_service_has_no_public_method_outside_the_catalog`
  （INV-52）: カタログ（`OPERATIONS`）と service のメソッドが過不足なく一致する。操作を足してメソッドを
  忘れる・カタログに無い公開メソッドが生える、の両方を落とす。
- 権限: `test_a_missing_scope_is_rejected_before_anything_is_sent`、`test_read_operations_need_read_scope`、
  `test_apply_needs_the_apply_scope_before_reading_the_change_set`: scope 検査が委譲の前にあること
  （権限の無い呼び出しで command が作られない・読み取りが走らない）。
  `test_start_production_is_human_only_even_with_the_scope`: scope があっても AI は有料の生成を始められない
  （`forbidden` / `human_only`、starter は呼ばれない）。
- 観測: 1操作=ちょうど1件（成功・`rejected`+code・`failed`）。`failed` の記録に例外の文面（秘密が混ざりうる）や
  入力の文面（hold の reason）が載らないこと。observer 無しでも動くこと。
- 冪等: 同じ `command_id` の再送は `created=False` で、`start` を再度呼ばない（二重起動しない）。
  draft は同じ Episode を返す。
- resume: agentic かつ flag OFF は command を作る**前**に `feature_disabled`（worker の無い workflow を
  投入しない / ADR-0044 §3）。legacy は flag OFF でも通る（日次の経路を変えない）。worker 確認の失敗は
  `unknown` で、受付済みの応答を失敗にしない。
- apply: `invalid_state` / `cost_not_estimated` / `budget_exceeded` は command を作らない。見積り `None` を
  0 円として扱わない（上限 0 でも通さない）。見積り=上限は通る（境界）。
- propose: 既存 use case へ委譲し、`correlation_id` が各 command に載る。

## 2. 層の境界（tests/architecture/test_layering.py）

- `test_application_layer_has_no_framework_or_io_imports`: application が fastapi・temporalio・sqlalchemy 等を
  import しない（既存のテストは自前の層間 import だけを見るため、第三者ライブラリは別に固定）。
