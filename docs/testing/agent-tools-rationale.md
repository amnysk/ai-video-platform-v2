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

## tests/contract/test_migration_0020_agent_tool_calls.py（migration 0020）

| テスト | 守るもの |
|---|---|
| 0019 時点の literal | downgrade 先の語彙が履歴として不変（enum が変わっても書き換わらない） |
| 凍結 literal == 現在の contracts | CHECK の語彙が `AgentRole` / `ArtifactType` / `TOOL_CALL_STATUSES` と食い違わない（INV-62 の同形） |
| 足した値は ADR の分だけ | `fact_check_report` / `historian` 以外を黙って足さない。シーン単位の集合は不変 |
| contracts を import しない | migration の凍結（後続の enum 変更で過去の migration が変わらない） |
| model と表の列 | ORM と migration の列・NULL 可否・UNIQUE(run, seq) の食い違い |
| round trip | upgrade/downgrade が式索引を失わず、表を作って消す |
| downgrade 拒否 | 新語彙の行・tool 呼び出しの行があれば何も変えず拒否（データを黙って消さない） |
| status CHECK | 語彙外の status（`invented`）を DB が拒否する |

既存テストの変更（ADR-0047 が enum に値を足すことの帰結。緩めていない）:
`test_migration_0019_agentic_vocabulary.py::test_frozen_literals_equal_the_current_enums_in_order`・
`test_migration_frozen_vocabulary.py::test_0014_upgrade_adds_exactly_the_adr_0035_values`・
`test_migration_0016_intervention.py::test_frozen_vocabulary_matches_the_contracts` は、
「現在の enum == 過去の migration の凍結語彙」を比べていたので、0020 が足した 1 値
（`fact_check_report` / `historian`）を明示的に除いて比べる。除く値は literal で、検査自体は同じ強さ。

## tests/contract/test_fact_check_report.py

| テスト | 守るもの |
|---|---|
| ARTIFACT_MODELS 登録 | `ArtifactType` を足したら model を登録する既存の完全性（`set(ARTIFACT_MODELS) == set(ArtifactType)`）を満たす |
| 根拠が要る | insufficient 以外の assessment は `evidence_refs` が必須。根拠が無ければ insufficient のまま（INV-58） |
| strict・untrusted | 余分な項目・`untrusted=False`・claim 13 件以上を拒否 |
