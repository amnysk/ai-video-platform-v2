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

## Analytics tool（ADR-0047 §5）

通信は全て `httpx.MockTransport`（実 YouTube に出ない / INV-18）。`tests/architecture/test_no_live_calls.py` は緑のまま
（Analytics の host 文字列は `youtube_analytics.py`、Data API の URL は `infrastructure/youtube/` にだけ置く）。

### tests/contract/test_agent_tools_analytics_contract.py

| テスト | 守るもの |
|---|---|
| 5 tool が read・youtube_quota・版 1 で登録 | 勝手な tool / 副作用 tool が増えない（INV-61/62）。Registry と定義が一致する |
| extra=forbid・frozen | LLM が作る入力に未知の項目を黙って通さない。出力の事後改変を防ぐ |
| video_id の形 | 任意の文字列（URL・query 断片）を API の filter に流さない |
| 範囲の順序 | start > end を API に送らない |
| compare は 2〜10 本・重複なし | 比較にならない入力と quota の浪費を契約で止める |

### tests/unit/test_analytics_derive.py（純粋な導出）

| テスト | 守るもの |
|---|---|
| 最大の落ち | 離脱要約が決定論的（大きい順・同値は前の位置・上昇と欠測は無視）。`derived` と名乗れる根拠 |
| age_days / aligned_window | 公開日を 0 日目に数える算術。compare の整列の土台 |
| shorts_views_affected | 2025-04-30 以前を含む窓だけ Shorts の views を信用しない |
| weighted_mean | 日別の平均を views で重み付けする。重み 0 は 0 でなく None |

### tests/unit/test_video_insights.py（adapter）

| テスト | 守るもの |
|---|---|
| tool ごとの query が完全一致 | 無効な指標・次元の組合せは HTTP 400。公式に確認した組合せ（ADR §5）から外れない |
| metric 部分指定・明示範囲は metadata を呼ばない | 無駄な Data API 呼び出し（quota）を避け、取れていない値は 0 でなく None |
| evidence_ref の安定性・秘匿 | 同じ query は同じ `analytics_query:<sha>`。token が証跡に入らない（INV-20） |
| impressions / CTR | 公式 API に無い値を作らず `not_available_via_api`。API も呼ばない |
| 空 rows = no_data | 0 と「行なし」を区別（INV-58） |
| 処理遅延内の範囲・公開直後 | 未確定日を確定値として返さない（`reporting_delay`） |
| 未知の動画 | `no_data(video_not_found)` |
| 旧窓の Shorts views 注記 | 2025-03-31 の定義変更を知らせる |
| HTTP 403 quotaExceeded / dailyLimitExceeded / rateLimitExceeded、429、400、5xx | 状態と短い code に写る。scope を疑う tokeninfo は quota / rate の 403 では呼ばない |
| 403 + scope 欠落 / 有 / tokeninfo 不可 | `permission_denied` の原因を区別（scope_missing / forbidden:<reason> / unverified） |
| 401 は 1 回だけ更新して再試行 | 既存 adapter と同じ扱い。2 回目の 401 で諦める（無限再試行しない） |
| timeout・通信失敗 | 例外でなく状態。例外文・repr に token / URL が出ない |
| 形の崩れ | `failed(malformed_response)`。例外を外へ出さない |
| retention | 位置順の points・derived の明示・空は no_data |
| demographics | age×gender と country の query。合算フィールドが無い。全欠測 = `withheld_by_threshold`（0 でない）。片方だけ欠測なら残りを返し limitations に残す |
| channel | totals・日別 trend・形式別・投稿数の query。search.list を使わない。ページング停止条件。日別の最終日が要求より前なら `reporting_delay` |
| compare 整列 | 公開日 + N-1 日の窓、最小経過日数 = N、グループは creatorContentType ごとで Shorts と長尺が混ざらない、順位はグループ内のみ |
| compare 重み付き平均 | 窓の平均は views 重み付きの derived |
| compare Shorts の views | 2025-04-30 以前を含む Shorts は views が insufficient_data（指標を落とし順位も付けない）。長尺は影響なし |
| compare 不足 | 1 グループ 1 本・全グループ不足・窓が短い・経過日数差が大きい・days 指定での除外・live/未知動画の除外 |
| 全リクエストが GET・許可 URL・query に token なし | read-only（INV-61）と INV-20 |
| 書き込み scope 文字列を持たない | 書き込み・収益 scope を要求する経路が無い |

### tests/unit/test_youtube_data_reader.py

| テスト | 守るもの |
|---|---|
| 尺の解析 | ISO 8601 → 秒。読めない値は None（0 にしない） |
| 公開日の UTC 正規化・読めない要素を捨てる | 日付境界の取り違えと、壊れた要素による誤計数 |
| 読み取り URL が 3 つだけ | 書き込み系の URL を足せない |

### tests/unit/test_analytics_tool_executors.py

| テスト | 守るもの |
|---|---|
| build が Registry と同じ 5 tool を返す（配線は test_agent_tools_wiring_executors が検査） | Registry と executor の不一致（INV-62） |
| tool ごとに対応する provider メソッドへ | 取り違え。`quota_units=unknown`（0 でない）を記録 |
| 日次上限 | 上限超過は外部を呼ばず `quota_exceeded(daily_tool_call_cap)`。翌日に戻る。tool 間で共有 |
| 入力 model の取り違え | 黙って別 tool を呼ばない |
| 設定 `AGENT_TOOLS_DAILY_ANALYTICS_CALLS` | 既定 50・正の整数だけ |

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

## tests/architecture/test_agent_tools_boundaries.py と test_agentic_default_off.py

| テスト | 守るもの |
|---|---|
| tool 識別子の literal 禁止（INV-62） | `"search_trends"` 等を別モジュールが書き直して集合が食い違うのを止める（AGENTS.md §8）。`ToolId` を import させる |
| literal 検査自体の検査 | 検査が空振りして何も見つけない状態を防ぐ |
| 書き込み scope・POST/PUT/DELETE/PATCH の禁止（INV-61） | `infrastructure/agents/tools/` に投稿・更新・収益の経路が入らない。既存の Planner 用 adapter は対象外と明記 |
| 契約・port の純粋さ | tool の契約と domain の port/gateway が infrastructure を import しない（層の逆流） |
| default-off（既存テストの強化） | `AGENTIC_MODULES` に tool の契約・`infrastructure.agents` を**足した**だけ（日次経路が tool を参照しない）。緩めていない |

## tests/unit/test_agent_tools_wiring_executors.py（統合）

| テスト | 守るもの |
|---|---|
| 日次上限は当日の analytics 行だけを数える | research や別の日の呼び出しで analytics が止まらない。gateway が先に begin するので今回分を含めて比べる |
| adapter 未注入なら executor を持たない | 組めない adapter を偽物で埋めず、gateway の拒否に任せる（INV-58） |
| extras は組めないものを入れない | OAuth 設定が無いのに provider を作らない・秘密を出さない |

## tests/unit/test_intervention_tool_evidence_target.py（人の指摘。INV-63）

| テスト | 守るもの |
|---|---|
| 新しい command 型は不要 | tool 呼び出しの指摘が `comment` の payload で表せ、`CommandType` が増えていない（ADR-0047 §7 の「新 command を作らない」） |
| `target_ref` は tool_call / decision + UUID だけ | 任意文字列・他の種類・注入文字を anchor に保存しない |
| flag は種類の合う target と review_finding を要する | `wrong_evidence` が decision を指す、target 無しの flag、単なる comment への flag を受けて一覧を壊さない |
| tool 呼び出しを `wrong_evidence` で指摘できる | 人の指摘が `annotations` に `anchor.target_ref` + `payload.flag` で残る |
| 不明・他 Episode の対象は拒否し何も書かない | 他 Episode の根拠への指摘の混入、存在しない対象の指摘 |
| 同じ command の再送は 1 行・1 event | INV-42。指摘の二重登録で未解決件数が増えない |
| 同じ command_id で内容違いは拒否 | 別の対象への指摘を黙って同じ指摘として扱わない |
| 判断を `wrong_decision` で差し戻せる | 承認 command が無い現状（Open）で出来ること |
