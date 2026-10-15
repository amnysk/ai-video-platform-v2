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

## 3. 読み取り adapter（tests/unit/test_episode_reader.py、SQLite + 記憶上の ArtifactStore）

OpenSearch・Temporal・MinIO の実体には触れない（PostgreSQL 相当の状態と ArtifactStore の読みだけ）。

- `test_a_missing_episode_is_episode_not_found_for_every_read`: 8 つの読み取りすべてが、Episode が無いとき
  同じ code で拒否する（port の契約。操作ごとに実装がずれていないこと）。
- `test_pipeline_kind_follows_the_pinned_plan`: agentic か legacy かは pin 済みの計画で決まる（resume の
  `feature_disabled` 判定の材料）。
- `test_list_is_newest_first_with_a_keyset_cursor_and_kinds`: 新しい順・`before` による次ページ・最後のページで
  `next_before=None`・一覧の種別が一括判定でも詳細と同じ。
- workspace: `workflow_id` は agentic/legacy で決定論的な id を返す。pending の change set は `proposed` だけで、
  未見積りを `None`（0 ではない）、見積りは4桁の文字列で返す。
- storyboard: 無ければ `None`、あれば現行 revision（`base_storyboard_revision_id` に渡す値）と編集可能な属性。
- change impact: 見積り・状態・`blockers`（`cost_not_estimated` / `invalid_state` / `stale_target_revision`）。
  古い版への変更が適用要求を出せると誤解されないこと。別 Episode・存在しない・UUID でない change set は
  `change_set_not_found`（他の Episode の変更案を覗けない）。
- quality review: Artifact が無いときは `not_reviewed`（失敗でも空の review でもない）。あるときは
  `self_assessed` のまま返る。
- progress: 失敗状態の job が `failure` になり、失敗が無ければ `None`。

## 4. 実 DB・実 bucket（tests/integration/test_jpmas2_p2a_reader.py）

`test_reader_against_real_postgres_and_minio`: SQLite では見えない timestamptz の keyset（`before`）・UUID 型・
実 bucket の JSON 読みを、使い捨てスキーマ + `TEST_MINIO_BUCKET` で通す。単体と同じ主張（種別・一覧の分割・
workflow id・storyboard・見積り 4 桁・別 Episode の change set は `change_set_not_found`）を言い直すだけで、
新しい仕様は足さない。
