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

## tests/contract/test_agent_spec_tools.py（AgentSpec.allowed_tool_ids）

| テスト | 守るもの |
|---|---|
| 1.0 は tool 無し・canonical にキーが出ない | 登録済み spec の `manifest_hash` が変わらない（INV-51）。空のときキーを出さないことが互換の要 |
| ADR の許可表（literal） | strategy@1.1 / learning@1.1 の tool が ADR-0047 §2 と一致。実装定数から導出せず、許可を黙って広げる変更を検出する（INV-58） |
| ref が ToolId に解決 | `"search_trends@1"` の綴り間違いを起動前（テスト時）に検出 |
| LLM 回数 <= 3 | tool は 1 往復（plan -> synthesize）。再帰の余地を spec の予算で潰す（INV-60） |
| tool 一覧の変更で hash が変わる | 許可の変更は新 spec_version を要求する（pin の意味） |
| 1.1 と 1.0 の差分 | 新版が version・LLM 回数・tool 以外を勝手に変えない |

`tests/contract/test_agent_registry.py` の `PINNED_HASHES` には 1.1 の 2 件を**足しただけ**（既存の値は不変）。
