# 未照合予約の照合: 各テストが存在する理由

対象: `domain/provider/reconciliation.py` / `ProviderReconciliationRepository` / `ProviderReservationRepository.reserve` の再送ガード /
`scripts/reconcile-reservations.py`（ADR-0051, INV-66）。実事故（2026-10-04 から宙吊りの fal 予約 1 件）から逆算した設計の穴を塞ぐ。

| テスト | 防ぐ誤判定・事故 |
|---|---|
| `test_classification`（COMPLETED で body 未読 → UNKNOWN の行） | fal の `COMPLETED` を成功と読む。本番の 403 はこの形（body を読めていない） |
| `test_no_observation_is_never_classified_as_failed` | 通信・認可エラーを「失敗」と読み、再送して二重課金する |
| `test_advance_is_idempotent` | 2 回実行して行が増える／判定が揺れる |
| `test_unknown_observation_never_overwrites_knowledge` | 後から来た読み取り失敗が、確定済みの知識を `unknown` に戻す |
| `test_terminal_verdicts_never_flip_to_another_terminal` | success と failed の食い違いを黙って上書きする（通報されない） |
| `test_blocks_new_round` | 課金済み／未確定の入力に新ラウンドを許す条件の取り違え（failed は許す側も固定） |
| `test_manual_resolution_requires_evidence_and_owner_approval` | 根拠も承認も無い手動確定（「緑にするための確定」） |
| `test_owner_approval_is_stored_as_a_fingerprint_not_the_token` | 承認 token が DB・ログに残る |
| `test_summary_is_collector_friendly` | preflight が provider 別・Episode 別に見られず、一律の止め／通しになる |
| `test_observation_is_idempotent_and_leaves_the_ledger_untouched` | 観測だけで `reserved` が自動で出る（INV-15 違反） |
| `test_manual_resolve_*` | 監査行が残らない／再実行で二重に書く／確定済みを別の結論へ変える |
| `test_confirmed_success_can_never_be_resubmitted_under_a_new_key` | 課金済み・結果ありの予約を、新 key で再送して二重課金する（`spent` で取得物が無いと `_plan_round` は次ラウンドを選ぶ） |
| `test_unknown_reservation_can_never_be_resubmitted_under_a_new_key` | 「分からない」を理由に再送する |
| `test_confirmed_failure_may_start_a_new_round` | ガードが強すぎて failed の通常 retry まで止める |
| `test_a_different_input_is_not_blocked` | ガードが入力を区別せず、プロンプトを直した新入力まで止める |
| `test_reconcile_is_dry_run_by_default_and_writes_nothing` | dry-run のつもりで台帳に書く |
| `test_reconcile_status_fn_exception_is_unknown_not_failure` | status 関数の例外が失敗扱いになる／例外メッセージ（秘密を含みうる）が出力に漏れる |
| `test_reconcile_without_a_status_source_refuses` | status 源が無いのに実 provider へ行く／黙って何もしない |
| `test_resolve_*` / `test_audit_row_never_stores_the_raw_token` | evidence・承認欠落の通過、dry-run の書き込み、token の漏洩 |
| `test_script_has_no_network_or_provider_imports` | CLI が owner 承認なしに実 provider へ接続する経路を持つ |
