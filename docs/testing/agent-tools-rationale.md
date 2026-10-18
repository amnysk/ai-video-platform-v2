# Agent Tools のテスト設計（ADR-0047）

各テストが「何を守るか」を残す（失敗したとき、テストを緩めるのではなく設計の食い違いを疑うため）。

## tests/contract/test_agent_tools_contract.py

| テスト | 守るもの |
|---|---|
| tool ID の語彙 | 依頼された 8 tool が過不足なく契約にある。勝手な tool が増えない |
| ref の往復 | `tool_id@version` の書式。`manifest_hash` に入る文字列なので曖昧さを残さない |
| read のみ（INV-61） | 投稿・削除・更新の tool を契約の段階で作れない |
| 上限が正 | timeout / 回数 0 の tool が「無制限」として登録されない |
| schema 導出 | JSON Schema が model と食い違わない（INV-62） |
| tool_call_id | retry で同じ ID になる（INV-59 の前提）。run・順序・tool・入力のどれが違っても別 ID |
| 状態の語彙（INV-58） | 非対応・データ不足・権限不足・反映遅延・閾値を区別して返せる |
| envelope | `data` は ok のときだけ。外部由来は常に `untrusted` |
