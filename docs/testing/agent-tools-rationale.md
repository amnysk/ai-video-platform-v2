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
| build が 5 tool を返す・EXECUTOR_BUILDERS に載る | Registry と executor の不一致（INV-62） |
| tool ごとに対応する provider メソッドへ | 取り違え。`quota_units=unknown`（0 でない）を記録 |
| 日次上限 | 上限超過は外部を呼ばず `quota_exceeded(daily_tool_call_cap)`。翌日に戻る。tool 間で共有 |
| 入力 model の取り違え | 黙って別 tool を呼ばない |
| 設定 `AGENT_TOOLS_DAILY_ANALYTICS_CALLS` | 既定 50・正の整数だけ |
