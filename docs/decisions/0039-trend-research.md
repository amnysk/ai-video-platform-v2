# ADR-0039: Trend Research を research の成果物として実装し、検証つきで読み出せるようにする

## Status

Accepted (2026-09-30)

範囲: Research Tier B の B5（Trend Research）。土台（依頼・台帳・Gateway・実行器・Worker）は ADR-0037、
Evidence は ADR-0038。Topic Planner への接続は B6 で、既定 OFF の opt-in としてこの ADR に追記して決める。

## Context

旧ブランチ `claude/research`（tip `8cede47`）の旧 ADR-0033「Trend Research・Topic Planner への接続・定期更新・
Provider 方針」は次を 1 つの ADR で決めていた。

1. §1 Trend Artifact（観測と解釈を分ける・単一の総合スコアを持たない・欠損を 0 にしない・差分と参考の平均を
   別の名前にする・`videoDuration` から Shorts と断定しない・視聴者は仮説）
2. §2 鮮度（`fresh` 24h / `stale` 7d / `none`）の純粋関数
3. §3 Topic Planner の `gather_context` が最新 Trend を読み、`topic_plans` に `trend_mode` / `trend_request_id` を足す
4. §4 定期更新: `TrendRefreshWorkflow` と Schedule `avp-trend-refresh`（`scripts/ensure-daily-schedule.py --trend`）、
   watchdog の `TREND_REFRESH_STALE` anomaly（旧 migration 0013 で `operational_anomalies.kind` を張り替え）
5. §5 Provider 方針（実 Provider は所有者が選ぶ。LLM の Port は Fake だけ）

本番系統（f209e7c 以降）は ADR-0037 で research の永続化を本番の表から切り離した。旧実装にはさらに次の問題が
あった。

- 解釈器（`TrendInterpreter`）の呼び出しが `TrendHandler.synthesize` の中の `await` で、予約台帳を通らない
  **予算外の LLM 呼び出し**だった（ADR-0038 が Evidence の評価器で塞いだ穴と同じ形）。
- 差分の増加速度は「直前の Trend」（`ctx.prior_trend`）を実行器が読んで Handler に渡す前提で、純粋な Handler の
  外に I/O の依存があった。
- Trend のテスト用 Fake（`FakeSearchProvider(stat_observed_at=)`・`fake_corpus.with_view_counts`・24 文字の
  channel id）は Tier A のファイルの変更（e7af6d5）で、base（ADR-0036）には入っていない。

**移植しないもの（決定）**:

- **定期更新の Schedule（`avp-trend-refresh`）と `TrendRefreshWorkflow`**: Schedule の作成・登録は本番の運用面
  （`infrastructure/temporal/schedules.py`、`scripts/ensure-daily-schedule.py`）に触れる。本番の Schedule と
  watchdog はこの移植で変えない（brief の Must NOT change）。Trend は**依頼ごと**（`POST /research/requests`）に走る。
- **`TREND_REFRESH_STALE` anomaly**: `contracts/schedule_guard.py`・`infrastructure/temporal/watchdog.py`・
  `workers/pipeline/watchdog.py` と `operational_anomalies.kind` の CHECK（本番の migration）を変える必要がある。
  base の watchdog は旧ブランチの分岐後に 4 段階へ作り直されており（ADR-0031）、Trend の監視を足すと本番の監視の
  意味が変わる。定期更新が無いので「古くなった」を監視する対象も無い。
- **`scripts/ensure-daily-schedule.py` の変更**: 上と同じ理由（本番の Schedule を作る運用スクリプト）。
- **Topic Planner への接続（旧 §3）**: B6 で opt-in（既定 OFF）として扱う。`topic_plans` に列を足さない
  （追跡が要れば research 側の表で持つ）。B5 は読み口（§4）だけを用意する。
- **直前の Trend を読んで作る差分**（旧 `ctx.prior_trend`）: §2 のとおり同じ依頼の中の複数時点だけで作る。
- **`fresh_until` を成果物に焼き込むこと**: 鮮度の窓は設定値で、読む側が判定する（§2）。
- **`partial` の Trend を読み口で使うこと**（旧 §2 は `completed` / `partial` を対象にした）: base の規則
  （INV-37・ADR-0037）どおり `partial` は合格ではない。

## Decision

### 1. 成果物の契約（`contracts/research_trend.py`）

`research_trend` は ADR-0037 の語彙（migration 0015 が凍結済み）の research 成果物で、新しい migration は
要らない。契約の validator が最終防衛線:

- **観測（`observations`）と解釈（`interpretations`）は別の欄**。観測は `observed_at` 必須で、`method` は指標名が
  決める（`views_per_hour_delta` → `delta`、`lifetime_average_views_per_hour` → `lifetime_average`、
  `hours_since_publish` → `elapsed`、ほかは `reading`）。解釈は `kind="hypothesis"` だけで、根拠の観測 ID を
  必ず持つ。**存在しない観測 ID・別の候補の観測を指す解釈、存在しない解釈を指す切り口は作れない**。
- **単一の総合スコア・順位の欄は無い**（欄名に `score` / `rank` を含まない。`extra=forbid`）。候補の指標は個別
  （`growth_observation` / `channel_scale` / `age_since_publish` / `theme_fit` / `difference_from_past` /
  `evidence_availability`）で、値が無ければ `status="unknown"` と理由（値・単位を持てない。**0 にしない**）。
- 形式は**依頼の値**（`format_basis="requested"`）で、確からしさ `format_confidence` は `low` に固定する
  （契約が強制）。候補に形式・長さの欄は無い（`videoDuration` / `duration_seconds` から Shorts と推定しない）。
- `audience_hypothesis` は `{text, kind="hypothesis", measured=false}`（測定値の顔をさせない）。`region` は
  `region_meaning="viewable_region_filter"`。
- 参照の `channel_id` は `YOUTUBE_CHANNEL_ID_PATTERN`（`contracts/upload.py`）。URL は http(s)・認証情報なし。
- 解釈器の出力 schema `InterpretationProposal` は strict（全 property required・`extra=forbid`）。

### 2. 増加速度と鮮度（純粋関数）

- `domain/research/trend_metrics.py::views_per_hour_delta` は**同じ動画を 2 時点以上**で観測したときだけ
  `known`（最も古い観測と最も新しい観測の差 ÷ 時間）。1 時点・同時刻の食い違い・減少・tz なしは `unknown`。
  Trend の検索計画は同じ語を 7 日と 30 日の窓で探すので、同じ依頼の中で同じ動画を 2 回観測しうる（Provider が
  返す `observed_at` が違えば差分になる）。1 時点しか無い動画は `lifetime_average_views_per_hour`
  （**参考値**。限界 `growth_is_lifetime_average` に明記）。
- `domain/research/trend_freshness.py::classify_trend_freshness(observed_at, now, fresh_hours=,
  stale_max_days=TREND_STALE_MAX_DAYS)` → `fresh` / `stale`（観測日時つき）/ `none`。`fresh_hours` は設定値
  `Settings.trend_fresh_hours` を呼び出し側が渡す（既定値は `contracts/research.py::TREND_FRESH_HOURS` だけ）。
  `TREND_STALE_MAX_DAYS = 7`（新しい定数。`contracts/research.py` が唯一の宣言元）。未来の観測は `none`。
  B6 の Planner がこれを使う。

### 3. 実行（`TrendHandler` と実行器の解釈の段）

- `TrendHandler`（`domain/research/trend_handler.py`）は ADR-0037 §8.3 の `ResearchHandler` と、新しい
  `InterpretingHandler.plan_interpretation`（`domain/research/handlers.py`）を実装し、registry が設定に依らず
  登録する（`none` の依頼は実行器の門で先に `blocked`）。
- 検索計画（`domain/research/trend_planning.py`）は旧実装と同じ意味（YouTube と Web、seed 語と周辺の話題、
  7 日と 30 日の窓、`region_code` / `language` は絞り込み、`shorts` は検索のヒント）。`max_searches` まで。
- **本文を取得しない**（`select_fetches` は空）。統計は検索結果にあり、テーマの適合は見出しと snippet で見る
  （取得の枠・費用を使わず、取得の一時障害で Trend 全体が retry / `partial` にならない）。
- **解釈器の呼び出しは実行器が台帳を通して行う**: `ResearchCall.ASSESS`、枠は `max_assessments`（INV-36）、
  書き込み順序・再実行の分岐・生データ（`raw/assess/`、`INTERPRET_CODEC`）は検索・評価と同じ。1 依頼 1 回。
  入力 hash は観測・候補・文脈の正準 JSON（`interpretation_input_payload`）。`synthesize` は外部を呼ばない。
- 解釈器の出力が schema に合わない（型に無い `overall_score`・観測の捏造）: `ResearchOutputInvalidError`
  （permanent）として穴にし、送り直さない。schema に合っても、存在しない観測 ID・別の候補の観測・総合スコアや
  順位の主張・参考値を「直近の伸び」と呼ぶ文・存在しない解釈を指す切り口があれば**提案ごと採用しない**
  （`adopt_interpretation`。修復しない）。どちらも Trend は観測だけの `partial`。
- 解釈器が組まれていない（`ResearchProviders.interpreter=None`）: 呼ばずに `assessor_not_available`
  （`ResearchStopCode` の既存の値。説明を Trend にも広げた）で `partial`。
- 実行器は Trend の成果物を契約に通し、候補の参照 URL がこの実行の検索結果の URL に属することを確かめてから
  保存する（Handler を信用しない）。
- 完了: 計画した検索がすべて結果を返し、統計の欠け・候補の上限超え・検索結果の警告が無く、観測があれば解釈を
  採用できたときだけ `completed`。それ以外は `partial`。
- registry: `fake` は `FakeTrendInterpreter` を組み、`none` は組まない。実 LLM は配線しない。

### 4. 読み口（B6 が使う）

`ResearchGateway.latest_trend(channel_id=, region=, language=, format_profile=None) -> VerifiedTrend | None`:

- `ResearchRequestRepository.list_completed(kind, channel_id)`（`completed` だけ、`as_of` → `finished_at` の
  新しい順）から、保存された依頼（契約を通す）の地域・言語（・形式）が一致する**最新の 1 件**を選ぶ。
- 結果（`result_summary`）が指す現行の `research_trend` 行を引き、キーが research のキーであること、
  ArtifactStore から読み戻した sha256 と読んだ本体の正準 JSON の sha256 が記録と一致すること、契約
  （`TrendArtifact`）を通ること、`request_id` が一致することを確かめて返す（base INV-31 / ADR-0037 §5 の考え方）。
- **どこで失敗しても `None`**（不一致・本体が無い・食い違い・DB / store の例外）。呼び出し側は「Trend 無し」
  として Trend 前の挙動で続ける。最新が検証に失敗しても**古い Trend に黙って戻らない**（鮮度の判断を誤らせない）。
- 鮮度では絞らない（`VerifiedTrend.observed_at` を §2 の関数に渡すのは読む側）。

### 5. 不変条件

新しい INV は足さない。「解釈器を予算外で呼ばない」は INV-36（全外部呼び出しが台帳の枠を通る）の適用で、
`tests/unit/test_research_trend_executor.py::test_the_interpretation_is_bounded_by_the_assessment_ceiling` が
行数を検査する。「観測と解釈を混同しない」「欠損を 0 にしない」「総合スコアを持たない」は成果物の契約の性質で、
`tests/contract/test_research_trend_contracts.py` が固定する（契約の validator が唯一の入口なので、INV として
別の層で守る対象が無い）。「読み口は検証できない Trend を返さない」は §4 の API の性質で、
`tests/unit/test_research_latest_trend.py` が固定する。本番への影響の無さは INV-37 のまま
（`tests/architecture/test_research_isolation.py` の対象に `contracts/research_trend.py` を足した）。

### 6. 既存テストの変更（この ADR が承認する）

- `tests/unit/test_research_registry.py` の `test_only_the_evidence_handler_is_registered`（B4）を
  `tests/unit/test_research_registry.py::test_exactly_the_evidence_and_trend_handlers_are_registered` に置き換えた。
  旧テストの docstring 自身が「Trend は後続の段（ADR-0039）で登録する」と予告していた仕様の変更。新しいテストは
  登録される種別が**ちょうど** Evidence と Trend であることと種別と Handler の対応を検査する（緩めていない）。
  未登録の種別の `blocked` は `tests/unit/test_research_executor.py::test_a_kind_without_a_handler_is_blocked`
  が引き続き検査する。ADR-0038 §6 の同じテストへの参照には追記で新しい名前を示した。
- `tests/architecture/test_research_isolation.py` の検査対象に `contracts/research_trend.py` を**足した**
  （検査が広がる側）。

### 7. Tier A の Fake の拡張（Tier A のファイルの変更）

Trend の検査に要る最小限を、既存の挙動を変えずに足した（旧 e7af6d5 と同じ意味）。既存の Tier A のテストは
変更していない。

- `infrastructure/research/fake_providers.py`: `FakeSearchProvider(stat_observed_at=)`（既定は従来の
  `OBSERVED_AT`）。同じ動画の別時点の観測を作る。
- `infrastructure/research/fake_corpus.py`: `with_view_counts(corpus, {url: views})`（元のコーパスは不変）。
  YouTube の資料の `channel_id` を `UC_hist_1` 等から本物の形（`UC` + 22 文字）に変えた。Trend の契約はこの形で
  ない id を参照から落とすので、Fake が本物と違う形のままだと Trend の検査が空振りする。旧 id を参照する
  テスト・コードは無い（grep 0 件）。

**読み手・書き手（AGENTS.md §8 の grep 記録、B5）**: `grep -rn "<key>" apps/ workers/ domain/ infrastructure/
contracts/ docs/` を B4 の commit（5b9c609）の上で、変更の前に実行した結果:

| キー | 件数 | 分類 |
|---|---|---|
| `TREND_STALE_MAX_DAYS` | 0 | 新規（`contracts/research.py` が唯一の定義。読み手は `domain/research/trend_freshness.py`） |
| `research_trend` | 3 | 定義 1（`contracts/research.py`）・migration 0015 の凍結 1・`docs/architecture/components.md` の説明 1。書き手は B5 の `TrendHandler`（実行器経由）だけ |
| `TrendInterpreter` / `latest_trend` / `classify_trend_freshness` | 0 / 0 / 0 | 新規 |
| `interpreter` | 1 | 無関係（`infrastructure/providers/piper_cli/synthesize.py` の Python interpreter の文言） |
| `trend_fresh_hours` | 6 | 設定の定義 1（`infrastructure/config.py`）・Gateway の再利用の窓 4・`docs/domain/research-request.md` 1。B5 で読み手は増やさない（`classify_trend_freshness` は引数で受け、B6 が設定値を渡す） |
| `TREND_FRESH_HOURS` | 8 | 定義と `__all__`・設定と Gateway の既定値・docs。値は変えない |

## Alternatives

- **(a) 旧実装どおり `synthesize` の中で解釈器を呼ぶ**: 台帳を通らない予算外の LLM 呼び出しになる（INV-36 の
  穴）。再実行で同じ解釈を送り直す。採らない（ADR-0038 (a) と同じ）。
- **(b) 解釈用に台帳の種別（`ResearchCall.INTERPRET`）を足す**: migration 0015 が凍結した `research_calls` の
  CHECK を張り替える必要がある。解釈も LLM の評価で、`max_assessments` の枠で足りる。採らない。
- **(c) Evidence の `AssessingHandler` / `AssessmentTask` を Trend にも流用する**: 評価の単位（claim と passage）と
  出力（`AssessmentProposal`）が Evidence の型で、Trend の観測を載せられない。別の Protocol
  （`InterpretingHandler`）にして実行器の骨格（`_run_call`）だけを共有する。
- **(d) 直前の Trend を読んで差分を作る（旧実装）**: 実行器が過去の成果物を読んで Handler に渡す I/O が増え、
  その Trend の検証（改ざん・世代）も要る。同じ依頼の中の複数時点で作れる差分だけにする。
- **(e) Web ページの本文を取得してテーマの適合に使う（旧実装）**: 取得の枠と費用を使い、取得の一時障害が Trend
  全体を retry / `partial` にする（ADR-0038 Consequences の規則）。見出しと snippet で足りる。採らない。
- **(f) 読み口が最新の検証失敗時に古い Trend へ戻る**: 壊れた最新を隠し、古いデータを新しいものとして使わせる。
  `None` にする。
- **(g) 定期更新・監視を今回移植する**: 本番の Schedule・watchdog・`operational_anomalies` を変える（Context）。
  採らない。

## Consequences

**良い**
- 解釈器の呼び出しも台帳の枠・生データ・再実行の分岐を通る。再実行は保存済みの提案を読み、送り直さない。
- 観測と解釈の混同・欠損の 0 埋め・総合スコア・長さからの Shorts 推定・測定値の顔をした視聴者仮説を、契約と
  採用の検査の二重で作れない。
- B6 は検証済みの Trend か `None` だけを受け取る。読み出しの失敗で Planner が止まらない。
- 本番の表・語彙・Schedule・watchdog は変わらない。migration も増えない。

**悪い / 引き受けた負債**
- **定期更新が無い**。Trend は誰かが依頼を出したときだけ新しくなり、放置すると `stale` → `none` になる
  （Planner は Trend 無しで続く）。定期更新・監視は本番の運用面の変更として所有者の判断と ADR を待つ。
- Fake の解釈器は意味を理解しない。Fake で検査しているのは契約・採用の規則・配線で、解釈の妥当性ではない。
- 差分は同じ依頼の中の複数時点だけ。実 Provider は検索のたびに観測時刻が変わるので、同じ動画の差分は数分〜数十分の
  間隔になりうる（値は正しいが短期の揺れを含む）。依頼をまたいだ差分は将来の変更。
- テーマの適合は見出しと snippet の語の一致で、近似。`difference_from_past` は過去の動画の**同一性**だけを見る
  （題材の近さは見ない）。
- 総合スコア・順位の文の検出は正規表現の近似（見逃しはありうる。検出すれば提案ごと捨てる安全側）。

## 追記 B6（2026-09-30）: Topic Planner への opt-in 接続

### Status

Accepted (2026-09-30)。Research Tier B の B6。**既定 OFF**。OFF のとき、また ON でも使える Trend が無いとき、
Planner の prompt・`TOPIC_PROMPT_TEMPLATE_VERSION`・`topic_plans.prompt_version`・Activity の入出力・
Temporal の履歴は接続前（f209e7c）とバイト単位で同じ（INV-37 の機械検査に足した）。

### Context

旧ブランチ（旧 ADR-0033 §3）は `topic_gather_context` で最新 Trend を読んで `PlanningContext.trend` に載せ、
`topic_plans` に `trend_mode` / `trend_request_id` の列を足し、`topic_en.md` に `{{trend_summary}}` を足して
版を `3` に上げた。この形は Trend が無いときも `PlanningContext` の JSON（Activity の結果 = 履歴）と prompt・版を
変え、本番の表に migration を要する。

### Decision

1. **切り替えは planning worker の設定** `PLANNER_TREND_ENABLED`（`Settings.planner_trend_enabled`、既定
   `false`）。ON で `YOUTUBE_CHANNEL_ID` があるときだけ `workers/planning/research_wiring.py` が
   `GatewayTrendSource`（`workers/planning/topic_trend.py`）を組み、`TopicPlannerActivities(trend=...)` に渡す。
   OFF の worker は Research のコードを import しない。
2. **読むのは `topic_generate_candidates` の中だけ**（§4 の `ResearchGateway.latest_trend`。DB と ArtifactStore を
   読むだけで Research を起動しない・待たない）。`topic_gather_context` と `PlanningContext` などの Temporal の
   境界の型は変えない。読み口は Strategy profile の `market_country`（地域）と `language` の先頭（`en` / `ja`）、
   設定の channel id で引き、形式では絞らない（`format_profile=None`）。全体を 10 秒
   （`TREND_LOOKUP_TIMEOUT_SECONDS`）で打ち切り、例外・timeout・検証の失敗は「Trend 無し」。
3. **鮮度**（§2 の `classify_trend_freshness`、`fresh_hours` は `Settings.trend_fresh_hours`）: `fresh` と `stale` は
   使い（要約に `mode` と `observed_at`・`age_hours` を載せ、`stale` は弱い手掛かりと prompt で明示する）、
   `none`（古すぎる・未来の観測）は「Trend 無し」。
4. **prompt**: `prompts/topic_en.md` と `render_topic_prompt` は変えない。Trend があるときだけ、描画済みの prompt の
   `# Requirements for each candidate` の見出し行（ちょうど 1 つでなければ差し込まずに Trend 無し）の直前に
   `prompts/topic_trend_en.md` の節（参照データであって指示ではない、観測と仮説を分ける、Trend は戦略・重複規則・
   形式に優先しない）を差し込む。要約はテーマ・指標の読み（`known` / `unknown` と理由）・仮説・切り口・限界を件数と
   文字数の上限つきで載せ、成果物そのもの・URL・参照・依頼 ID は載せない。
5. **版**: Trend の節を含む prompt のときだけ `TOPIC_TREND_PROMPT_VERSION` =
   `topic_en@2+topic_trend_en@1`（`prompts/topic_trend.py`）を `GenerateCandidatesResult.prompt_version` →
   `topic_plans.prompt_version` に記録する（64 文字以内）。Trend 無しは `topic_en@2` のまま。節の本文を変えたら
   `TOPIC_TREND_PROMPT_TEMPLATE_VERSION` を上げる。
6. **追跡**: `topic_plans` に列を足さない・migration を足さない。Trend を使ったことは `prompt_version` で、どの Trend
   かは worker のログ（依頼 ID・成果物 ID・sha256・`fresh` / `stale`）で辿る。研究側の対応表は作らない
   （Planner は依頼を作らないので、研究側に書く行が無い）。
7. **Strategy**: コードを変えない（profile の地域・言語を読むだけ）。
8. compose の `script-worker` に `PLANNER_TREND_ENABLED`（既定 `false`）を渡す。Trend の依頼そのものは従来どおり
   `POST /research/requests`（定期更新は移植していない）。

**読み手・書き手（AGENTS.md §8 の grep 記録、B6）**: `git grep -n "<key>" 81fa6d6 -- apps workers domain
infrastructure contracts docs compose.yaml prompts`:

| キー | 件数 | 分類 |
|---|---|---|
| `planner_trend_enabled` / `PLANNER_TREND_ENABLED` | 0 / 0 | 新規（`infrastructure/config.py` が唯一の定義。読み手は `run_worker.py` / `research_wiring.py`、書き手は compose の script-worker） |
| `topic_trend_en` / `TOPIC_TREND` | 0 / 0 | 新規（`prompts/topic_trend.py` が唯一の定義） |
| `trend_fresh_hours` | 10 | 設定の定義・Gateway の再利用の窓・設計書。B6 で読み手に `research_wiring.py`（`GatewayTrendSource` へ渡す）を足した |
| `latest_trend` | 13 | 定義（Gateway）・その検査・設計書。B6 で読み手に `topic_trend.py` を足した |

### Alternatives

- **(a) 旧実装どおり `PlanningContext` に `trend` を足し `topic_en.md` に `{{trend_summary}}` を足す**: Trend が無くても
  履歴の JSON と prompt・版が変わる。採らない。
- **(b) `topic_plans` に `trend_mode` / `trend_request_id` を足す（旧 migration 0012）**: 本番の表と migration を変える。
  採らない（`prompt_version` とログ）。
- **(c) `topic_trend_en.md` を `topic_en.md` の全文の写しにする**: 同じ本文を 2 か所に持ち、片方だけ直す事故を招く
  （AGENTS.md §8）。採らない（節だけを差し込む）。
- **(d) `stale` を使わない**: §2 は `stale` を観測日時つきで使うと決めている。定期更新が無い（Context）ので、`stale` を
  捨てると多くの日で Trend が使えない。`mode` を明示して弱い手掛かりとして使う。

### Consequences

- 良い: OFF・Trend 無しの Planner は接続前とバイト単位で同じ（golden で検査）。Trend の障害・遅延で Planner は
  止まらない（最大 10 秒で諦める）。
- 悪い: Trend を使った plan は `prompt_version` だけが印で、どの Trend かはログにしか残らない。round ごとに読み直すので、
  round の間に新しい Trend が完了すると round ごとに別の Trend を見うる（記録される版は最後の round のもの）。
  地域・言語は Strategy profile から機械的に決めるので、別の地域の Trend を使うには profile を変える必要がある。
