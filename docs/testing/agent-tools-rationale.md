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

## Research tool（`contracts/agent_tools_research.py`、`infrastructure/agents/tools/research.py`）

既存の Research（Gateway・台帳・Fake Provider）を呼ぶ adapter。テストは SQLite・インメモリ store・Fake
だけで走り、実 API・実ネットワークには出ない（Docker の integration は対象外）。

### tests/unit/test_agent_tools_research_contract.py

| テスト | 守るもの |
|---|---|
| 3 tool の登録 | read-only・`research_ledger`・上限が正・入出力 model が `extra=forbid`（INV-58/61/62） |
| search_trends の入力 | region・seed_terms・仮説文の上限を Research の契約と同じ値で検査（定義は Research 側の 1 箇所を import） |
| claims の入力 | ≤12 件・300 字・重複・不正な kind を実行前に拒否 |
| fetch_sources の入力 | 参照が無い入力は拒否。`url` は受け取るが `not_supported` を返すためだけの欄 |

### tests/unit/test_agent_tools_research_executors.py

| テスト | 守るもの |
|---|---|
| trends の要約 | 候補・出典参照・仮説（`kind=hypothesis`、`measured=False`）を返し、`audience_is_hypothesis` / `provider=fake` を明示。requester = `agent:<ref>`、冪等キー = `ctx.idempotency_key`、`research_request:` / `research_artifact:` の参照 |
| trends に本文なし | 要約のみ（INV-60） |
| 評価の素通し | Research の `assessment` を変えず、数値の信頼度・スコアを作らない（INV-58） |
| 注入文の遮断 | 取得した本文に命令文を仕込み（成果物に入っていることを前提として確認）、tool 出力・参照ビューに現れない（INV-60） |
| fetch_sources の参照ビュー | 成果物の資料と一致し、抜粋を含まず、新しい依頼・呼び出しを作らない |
| 任意 URL | `not_supported`。Gateway・Provider に触れない |
| 未知・Trend の依頼 | `no_data` / `not_supported`（架空の一覧を作らない） |
| Activity retry（Evidence / Trend） | 同じ ctx の再実行で依頼は 1 件、`research_calls` が増えない（二重課金なし。INV-59） |
| pending → 同じキーで再開 | 有界ポーリング（回数固定）・heartbeat・`pending` で戻り、同じ依頼が完了する。依頼・台帳は増えない |
| provider none / budget_not_set / ambiguous_call | `provider_not_configured` / `failed`+error_code。台帳に行を作らず外部を呼ばない |
| failed / blocked(未知の型) | `failed` + `execution_failed`。データを返さない |
| partial | 使える観測がある Trend は `ok`+`research_partial`、評価が 1 件も無い Evidence は `insufficient_data`（partial を合格にしない） |

### tests/architecture/test_agent_tools_research_boundary.py

| テスト | 守るもの |
|---|---|
| adapter の隔離 | adapter も本番の課金・リポジトリ・表名・実 Provider（`HttpContentFetcher` 等）に触れない。既存の隔離規則（INV-37）は緩めていない |
| 本番工程から参照されない | Episode 本番工程が adapter を import しない（Research を待つ経路を作らない） |
