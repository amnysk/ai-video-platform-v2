# scripts/quality-approve.py: テストを置いた理由（ADR-0081 §9a / INV-129）

`tests/unit/test_quality_approve_script.py`。実 sqlite + メモリ store + fake の workflow starter。Temporal・DB 本番・YouTube・fal には触れない。

| テスト | 防ぐ失敗 |
|---|---|
| `test_only_the_owners_own_ref_is_accepted` | エージェントの委任参照（`AGENT-…`）・空・小文字・実在しない日付・前後空白・形違いで REVIEW が解放される |
| `test_dry_run_prints_the_outcome_and_writes_and_starts_nothing` | 既定の実行で承認行や workflow 起動が起きる（誤操作の事故） |
| `test_block_writes_nothing_and_starts_nothing` | BLOCK が所有者の参照で解放される |
| `test_a_gate_that_raises_writes_nothing_and_starts_nothing` | 評価の例外が PASS に倒れる |
| `test_a_failed_approval_write_starts_no_workflow` | 承認の記録が無いまま投稿が始まる（監査の欠落） |
| `test_success_records_one_owner_approval_and_starts_the_api_workflow` | 承認が 0 / 2 行になる、種別が owner でない、API と別の workflow id で起動して二重投稿の防止が効かない |
| `test_an_existing_upload_receipt_is_refused` | 投稿済みの動画を再投稿する |
| `test_without_a_service_the_flag_is_off_and_nothing_happens` | flag OFF で記録の表に触れる（INV-130） |
| `test_a_non_uuid_episode_id_is_refused` | 不正な ID で DB 問い合わせ・起動に進む |
