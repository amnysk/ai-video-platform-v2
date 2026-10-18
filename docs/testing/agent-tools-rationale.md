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

## tests/unit/test_agent_tool_call_gateway.py（ToolGateway.call_tool）

fake の tool 定義・executor・store（`tests/support/agent_tools.py`）だけを使う。実際の Research / Analytics は呼ばない。

| テスト | 守るもの |
|---|---|
| 許可外・未登録・綴り不正・未供給は executor を呼ぶ前に拒否 | INV-58。拒否は `refused` として記録し、store に行も作らない（外部への副作用ゼロ） |
| 入力 schema 違反 | LLM が作った入力で外部を呼ばない。記録にも入力本文を残さない |
| `max_calls_per_run` | tool ごとの上限。超過は呼ぶ前に拒否 |
| ID・seq は契約の関数から | `tool_call_id` が retry で変わらない（INV-59 の前提）。executor には `tool:{id}` の冪等キーが渡る |
| 成功は untrusted envelope | データは output_model で検証した上で dict 化。入力は検証済みのものを store に残す |
| retry は保存済みを返し executor 0 回 | INV-59 の本体。ok 以外の状態（quota 超過）も再利用する |
| started のまま = youtube_quota は再送しない | 呼んだか不明な行を再送して quota を二重に使わない。ambiguous を確定させ、次の retry も呼ばない |
| started のまま = research_ledger は同じ冪等キーで再実行 | Research の台帳が二重課金を防ぐので、結果を得るために再び呼ぶ（行は作り直さない） |
| timeout | `asyncio.wait_for` で状態 `timeout`。例外にしない |
| executor の例外 | 状態 `failed`、コードは型名だけ（例外メッセージに混ざる token・本文を残さない） |
| 出力が output_model に違反 | `failed`。壊れた値を ok として渡さない（INV-58） |
| ok 以外に data がある / ok に data が無い | executor の契約違反を `failed` にする。架空の値で埋めない |
| 状態の素通し | 欠測・遅延・権限不足などが区別されたまま Agent に届く |
| invocations / observer に本文なし | 入力・結果の秘密文字列が `agent_runs.tool_invocations` と観測イベントに出ない |
| observer の故障 | 観測の失敗で業務の結果が変わらない（INV-38） |
| runtime 無し（flag OFF） | `call_tool` は全て拒否。既存の `llm_json` の記録の形は不変（INV-55 の延長） |
| `render_untrusted` | `<data>` 区切りで、本文中の `</data>` を閉じ文字として残さない。handler と同じ実装（`data_block`）を使う |

## tests/unit/test_agent_tool_call_repository.py（AgentToolCallRepository）

SQLite + インメモリ ArtifactStore。PostgreSQL 固有の挙動は integration（Docker が要るためここでは走らせない）。

| テスト | 守るもの |
|---|---|
| begin は started を commit | 実行前に「呼ぶ予定」が残る。落ちたあとの retry が ambiguous を判定できる前提（INV-59） |
| begin の二重 | 同じ ID の再 begin は no-op（retry で行が増えない） |
| 同じ (run, seq) に別 ID | handler が決定論でない場合は握りつぶさず失敗（UNIQUE が効く） |
| complete は本文を MinIO、行に参照と sha256 | 外部由来の本文を DB に置かない。行・状態・evidence_refs・研究依頼 ID が残る |
| 改ざん検出 | 読み戻した sha256 が行と違えば `ToolCallIntegrityError`（壊れた結果を再利用しない） |
| started が無い complete | 行が無い完了を許さない |
| gateway + 実 repository の retry | executor 0 回・行 1 件（INV-59 を永続化込みで確認） |
| ambiguous youtube_quota | started 行が残っていれば再送せず failed を確定し、行にも `ambiguous_prior_attempt` が残る |

## tests/unit/test_agent_tools_wiring.py（flag・配線・観測）

| テスト | 守るもの |
|---|---|
| 既定は全て OFF | `JPMAS2_AGENT_TOOLS=false`・`JPMAS2_ENABLED_TOOLS` 空（INV-55 の延長。`test_agentic_flags.py` と同形） |
| カンマ区切り・未知の ID は起動時に失敗 | 誤記で黙って何も有効にならない（`JPMAS2_ENABLED_AGENTS` と同じ扱い） |
| flag OFF なら support を作らない | OFF で executor・store・observer を組み立てない。有効な tool が設定されていても |
| flag ON は DB store と logging observer | 本番の組み合わせが配線される。実 tool が無いうちは executor は空 |
| 有効な tool だけ executor を持つ | `JPMAS2_ENABLED_TOOLS` に無い tool は gateway が `tool_not_provided` で拒否できる |
| 二つの tool 群が同じ tool を登録 | 起動時に失敗（並行開発の取り違えを黙って上書きしない） |
| strategy@1.1 の stage が tool を 1 回呼ぶ・replay は 0 回 | Activity 配線込みで INV-59。行は `ok / seq=1 / strategy / 1.1`、本文は行に無い |
| stage の保存前に落ちた retry | 同じ run_key・同じ入力で同じ `tool_call_id` になり、保存済みの結果を使って外部を呼ばない |
| support 無し（flag OFF）の worker | tool 呼び出しは `ToolNotAllowedError`（非再試行）、`agent_tool_calls` は 0 行 |
| 1.0 の stage | tool support があっても tool を持たない（既存 spec の挙動は不変） |
| logging observer | ID・状態・時間・属性だけ。入力・結果の秘密文字列が LogRecord に無い。実 stage でも同じ |
| log 故障 | observer の発行が壊れても業務に伝播しない（INV-38） |
| compose の pass-through | agent-worker が既定 OFF で flag を受け取る |
