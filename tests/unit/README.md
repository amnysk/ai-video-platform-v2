# unit tests

domain/ の純粋ロジック。DB・ネットワーク・Docker に依存しない。

Phase 1 で最初に置くもの:
- `test_state_transitions.py` — 遷移表の全網羅（docs/domain/state-transitions.md）
- `test_failure_policy.py` — 失敗クラス分類（INV-12）
- `test_upload_idempotency.py` — 冪等キーの規則（INV-14）
