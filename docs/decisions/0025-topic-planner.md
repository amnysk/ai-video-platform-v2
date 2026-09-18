# ADR-0025: Topic Planner（TopicPlan を確定してから Episode を作る）

## Status

Accepted (2026-09-18)

## Context

ADR-0023 の `DailyEpisodeWorkflow` は Episode を作るが、Schedule の入力に topic は無く、台本は
「トピック未指定」で生成されていた。毎日自動で作るには「何を作るか」を決める工程が要る。制約と実情:

- **LLM（Codex CLI）は planning worker（queue `script`）にしか無い**。pipeline worker のコンテナには
  LLM も認証情報も無い（ADR-0024）。INV-3 により pipeline worker が planning worker を import することもできない
- 重複トピックを作らないこと。「昨日 planned になったがまだ投稿されていない」Episode も重複の対象に入れる
  必要がある（投稿済みだけを見ると、制作中の 2 本が同じ題材になる）
- Daily の再実行・Activity の再試行・クラッシュで**別の Topic が生まれてはならない**（INV-17、ADR-0021 の日次枠）
- 前身 repo の Analytics 取得は `youtube-analytics-mcp` 経由だったが、そのソースは失われ `.pyc` しか残っていない
- 現在の OAuth refresh token は upload 用 scope のみで、`https://www.googleapis.com/auth/yt-analytics.readonly`
  は**まだ付与されていない**
- PostgreSQL は `postgres:17-alpine`（pgvector 無し）。unit test は同じモデルを SQLite で動かす
- 形式（Shorts / Long）をコアに埋め込まない（ADR-0023 の `render_profile_id` と同じ方針）

## Decision

**Daily は Episode を作る前に子 workflow `TopicPlannerWorkflow`（queue `script`）で TopicPlan を 1 件確定し、
その id を持つ Episode だけを pipeline に流す。**

1. **順序**（`DailyEpisodeWorkflow`、queue `pipeline`）:
   `pipeline_check_paused` → 子 `TopicPlannerWorkflow`（`TopicPlannerInput(plan_date=slot_date,
   strategy_profile_id, content_profile_id)`）→ `pipeline_claim_daily_slot`（`topic` と `topic_plan_id` を渡す）
   → `claim.topic_plan_id` が非 None のときだけ子 `EpisodePipelineWorkflow` を起動。
   Planner が失敗したら Daily も失敗し、Episode も pipeline も作らない（INV-21）
2. **Planner は queue `script` の子 workflow**。Codex を持つのは planning worker だけで、pipeline のコンテナに LLM を
   持ち込むより、Temporal の queue で planning worker へ仕事を渡す方が INV-3 / INV-4 と既存の配置に沿う。
   workflow 名・queue・id 規約は `contracts/topic_planning.py`（`TOPIC_PLANNER_WORKFLOW` /
   `topic_plan_workflow_id`）
3. **TopicPlannerWorkflow**:
   `topic_find_plan`（既存 Plan があれば `reused=True` で即返す）→ `topic_gather_context`（Analytics + Content
   Memory）→ `policy.max_rounds` までのラウンド: `topic_generate_candidates`（LLM、`maximum_attempts=1`、
   `TopicCandidateBatch` で validation）→ `topic_select_and_save`（決定論的な重複判定・採点・選択を 1 トランザクションで保存）。
   全候補が落ちたら `avoid_subjects` を付けて次ラウンド。全ラウンドを使い切ったら non-retryable で失敗する
4. **冪等性（INV-22）** は 3 層で持つ:
   - DB: `topic_plans` の `UNIQUE(plan_date, strategy_profile_id, content_profile_id)`（`uq_topic_plans_day_profile`）。
     `episodes.topic_plan_id` も UNIQUE（1 Plan = 1 Episode）
   - Temporal: 子 workflow id `topic-plan-{date}-{strategy}-{content}` は決定論的。id reuse は ALLOW_DUPLICATE
     （再実行は DB から Plan を見つけて即返す）。走行中なら（`WorkflowAlreadyStartedError`）Daily は
     `workflow.sleep` して待ち直す。待ち時間の合計は Planner の execution timeout
     （`TOPIC_PLANNER_EXECUTION_TIMEOUT_SECONDS`、`contracts/topic_planning.py`）以上になるよう回数を導出し、
     使い切ったら non-retryable で失敗する
   - Activity: find-first（`topic_find_plan` と `topic_select_and_save` の先頭で既存 Plan を探す）。
     UNIQUE 違反で負けた側は既存 Plan を返す
5. **1 日 1 Plan / profile の組**。`DAILY_EPISODE_LIMIT > 1` でも同じ profile の組なら Plan は 1 件なので、
   2 本目以降は作られない（**既知の制約**。下の Consequences）
6. **重複判定（INV-24）**: Content Memory（`topic_plans` 全件 + `cancelled` 以外で topic を持つ全 Episode。
   別テーブルは作らず PostgreSQL から導出）に対し、domain の純粋関数で段階判定する。
   | Level | 条件 | 扱い |
   |---|---|---|
   | L1 `exact` | 同じ subject かつ同じ angle / 正規化タイトルのトークン集合が一致 | reject |
   | L2 `semantic` | 類似度 ≥ `similarity_reject` | reject |
   | L3 `same_subject` | 同じ subject・別 angle | `same_subject_cooldown_days` 内は reject、外は penalty |
   | `none` | 上記以外 | 類似度の帯（strong / mild）で penalty |
   類似度は正規化した特徴（subject / entities / theme / era / angle の重み付き一致）とタイトルトークンの
   Jaccard の max。同じバッチ内の候補同士も重複判定する
7. **pgvector は使わない**。`postgres:17-alpine` に入っておらず、SQLite との parity（unit test）が崩れる。
   埋め込みによる意味的類似は将来の選択肢（Level 2 の実装を差し替える）
8. **採点**（決定論、`PlannerPolicy.weights`）: `analytics_fit` / `us_young_fit` / `novelty` / `portfolio_balance` /
   `production_fit`。`analytics_confidence = min(1, views_28d/confidence_full_views) × min(1, videos/confidence_full_videos)`
   で analytics の重みを縮め、縮めた分は他の重みへ比例配分する（合計 1）。最終点 = 加重和 − 重複 penalty。
   同点は ordinal が小さい方。内訳（5 成分 + confidence + penalty）を `score_breakdown` に保存する
9. **過学習の抑制**: Analytics は「上位動画の題材を真似る」ためでなく、theme / angle / era / subject ごとの
   相対成績（特徴量）に変換して使う。novelty・cooldown・portfolio balance が逆向きに効き、データが少なければ
   confidence が重みを小さくする
10. **Analytics の fallback**（`topic_gather_context`）: live 取得成功 → snapshot 保存（UNIQUE(date, provider)）→
    `normal`。失敗 → 最新の保存済み snapshot → `stale_analytics`。snapshot も無い → `no_analytics`。
    使った mode・snapshot・confidence を `topic_plans` に記録する。DB の失敗は握りつぶさない
11. **Analytics は `AnalyticsProvider` port**（`domain/topic_planning/ports.py`）の背後に置き、adapter は
    YouTube Analytics API v2 `reports.query` を直接呼ぶ（`infrastructure/analytics/youtube_analytics.py`、既存の OAuth
    refresh、scope `yt-analytics.readonly`）。コアは MCP に依存しない
12. **Codex の Topic 呼び出しは予約台帳（ADR-0013）に載せない**。`provider_reservations` は Episode 単位だが
    Topic 生成時には Episode がまだ無い。呼び出し回数は `policy.max_rounds`（既定 3）で上から抑える
13. **SSoT（AGENTS.md §8）**:
    | 値 | 唯一の宣言元 | 他の場所 |
    |---|---|---|
    | Strategy / Content profile の中身・version | `contracts/topic_planning.py`（`STRATEGY_PROFILES` / `CONTENT_PROFILES`） | — |
    | 重み・閾値・cooldown・候補数・ラウンド数 | `contracts/topic_planning.py`（`PlannerPolicy`） | — |
    | ロジックの version | `PLANNER_VERSION`、prompt の version は `prompts` | DB に記録 |
    | どの profile を使うか | `Settings`（`TOPIC_STRATEGY_PROFILE_ID` / `TOPIC_CONTENT_PROFILE_ID`）。**id だけ** | Schedule 入力 |
    | 採用時点の id / version / analytics mode | `topic_plans`（PostgreSQL） | — |
    | prompt | profile を**データ**として受け取る（profile の値を prompt に直書きしない） | — |
14. **Shorts をハードコードしない**。Planner のコアは `ContentProfile` の中身で分岐せず、`format_brief` を
    prompt に渡すだけ
15. **LLM 出力は契約を通してから保存する（INV-23）**。`TopicCandidate`（`extra="forbid"`）に通らない候補は DB に
    入れない。形式不正は ADR-0014 に従い retryable（次ラウンド）

## Alternatives

- **pipeline worker に LLM を持たせて Activity で企画する**: pipeline コンテナに Codex と認証を持ち込むことになり、
  ADR-0024 の配置と崩れる。planning worker に同じ Codex 実行基盤が既にある
- **Daily の入力 / Settings に topic を書く**: 人手が毎日要る。重複を防げない
- **Episode を先に作り、後から topic を埋める**: topic の無い Episode が pipeline に流れうる（INV-21 違反）。
  失敗時に空 Episode が残る
- **Temporal の workflow id だけで 1 日 1 Plan を保つ**: id reuse・終了後の再実行で破れる。DB の UNIQUE を権威にする
- **pgvector で埋め込み類似**: 拡張の導入と SQLite parity の喪失。決定論的な特徴類似で当面足りる
- **Analytics 上位の題材をそのまま推す**: 同じ題材の再生産（過学習）になる。特徴量化 + novelty で抑える
- **MCP サーバ経由で Analytics を取る**: ソースが失われており保守できない。API を直接呼ぶ adapter の方が小さい
- **Topic 生成も予約台帳に載せる**: 台帳のキーが Episode 単位で、Episode 作成前に予約できない。
  Episode 非依存の台帳は別設計になり、今回の範囲を超える
- **Analytics の取得失敗で Planner を止める**: scope 未付与の間は毎日止まる。fallback で企画は続ける

## Consequences

- 良い: 自動生成 Episode は必ず Plan を持ち、重複・再実行で Topic が増えない。採用理由（score 内訳・analytics mode・
  version）が DB に残る
- **planning worker（queue `script`）の再デプロイが必要**。新しい workflow / Activity を登録していない古い worker では
  Daily が子 workflow で止まる
- **既知の制約**: 同じ profile の組では 1 日 1 Plan なので、`DAILY_EPISODE_LIMIT > 1` は実質 1 に上限される。
  複数本にするには別の profile の組を使うか、Plan の鍵に slot 番号を加える（将来の ADR）
- **引き受けた負債**: Topic 用 Codex 呼び出しは予約台帳の外にある。クラッシュ時の再送は台帳で抑止されず、
  1 回の Planner で最大 `max_rounds` 回、再実行ごとにさらに増えうる（Plan が確定していれば find-first で 0 回）。
  Codex は定額枠なので金額の暴走ではなく枠の消費として受け入れる
- **`yt-analytics.readonly` を再認可するまで Planner は `no_analytics`（snapshot があれば `stale_analytics`）で動く**。
  analytics_fit は confidence 0 で採点に効かない。再認可は運用者の作業
- 意味的重複は特徴量とタイトルトークンの近似で、言い換えを見逃しうる。pgvector 導入時に Level 2 を差し替える
- Planner の失敗で Daily が失敗した日は Episode が作られない（後で手動 trigger すれば同日の Plan を作り直す）
- 子 Planner には execution timeout（`TOPIC_PLANNER_EXECUTION_TIMEOUT_SECONDS` = 90 分）を付ける。Daily は
  Planner の完了を待つので、**Schedule の overlap SKIP は Planner の実行時間まで広がる**（その間の次の trigger は捨てる）
- **Workflow の versioning**: Planner の呼び出しは `workflow.patched("topic-planner-0025")` で囲む。marker の無い
  （導入前に始まった）履歴は旧経路（Planner 無し・`request.topic` で claim・plan の有無を見ずに pipeline 起動）で
  replay し、非決定性エラーにしない（`tests/unit/test_pipeline_workflows.py` の旧履歴 replay テスト）。
  導入時は Schedule を pause → 走行中の Daily が無いことを確認 → deploy → unpause の順で入れる
  （`docs/operations/pipeline-worker.md`）

## 追記: 旧入力 `DailyEpisodeInput.topic` と Topic の長さ（2026-09-19）

- **`DailyEpisodeInput.topic` は削除せず deprecated として残す。** Temporal は既存 Schedule の action input と
  実行中・完了済みの履歴をこの dataclass で decode する。field を消すと旧 payload の decode・replay を壊しうる。
  patched 経路（`topic-planner-0025` の marker あり）は値を**読まない**（Topic は Plan から取る）。
  marker の無い旧履歴の replay だけが `request.topic` で claim する。旧経路を `deprecate_patch` で消した後、
  Schedule の入力を更新してから field を削除できる（`tests/unit/test_pipeline_schedule.py` に topic 入り payload の
  decode テスト）
- **Topic の上限は `contracts/topic.py::TOPIC_MAX_CHARS`（200 字）を単一の宣言元にする。** ScriptArtifact・API
  （`CreateEpisodeRequest`）・`episodes.topic` 列がこれに従う。列は migration 0009 で `String(500)` → `String(200)`
  （本番の最大長は 24 字で、切り詰めは起きない。200 超の行があれば ALTER が失敗して止まる）

