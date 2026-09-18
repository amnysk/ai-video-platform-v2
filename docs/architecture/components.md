# コンポーネント

各コンポーネントについて「持つもの／持たないもの／呼んでよい相手」を定義する。
「持たないもの」の欄が本体である ── 責務の漏れはここに書いてある禁止事項の違反として現れる。

## apps/web （Next.js + TypeScript）

- **持つ**: 画面、フォーム検証、表示用の派生計算
- **持たない**: ドメインロジック、状態遷移の判断、Temporalクライアント、DB接続
- **呼んでよい**: `apps/api` のHTTP APIのみ（INV-1）

## apps/api （FastAPI）

- **持つ**: HTTPルーティング、リクエスト検証、認可、
  DBへのcommand永続化、Temporal workflow の start / signal / query
- **持たない**: 動画生成・レンダリング・アップロード・外部AI呼び出しの同期実行（INV-16）、
  工程の順序判断
- **呼んでよい**: `domain`, `contracts`, `infrastructure`
- ハンドラの本体は「検証 → domainのcommandを作る → 永続化 → workflowへ渡す → 202を返す」

## Temporal workflow 定義（`workers/<stage>/workflows.py`）

- **持つ**: 工程の順序、RetryPolicy、timeout、signal待ち、補償処理
- **持たない**: I/O（全てActivity経由）、非決定的な処理
- ここが「次に何をするか」を知る**唯一の場所**（INV-4）

## workers/* （Temporal Activity）

1工程 = 1モジュール。実装済みのもの（`pipeline` は順序だけを持つ workflow と状態系 Activity。ADR-0023）:

| worker | 入力Artifact | 出力Artifact | 外部副作用 |
|---|---|---|---|
| `planning` | （Topic Planner）Content Memory（`topic_plans` + `episodes`）・Analytics snapshot / （台本）TopicPlan の topic | `topic_plans` / `topic_candidates` 行（ADR-0025）/ `script` | LLM（Codex CLI）。Topic 生成は予約台帳の外（ADR-0025 §12）/ YouTube Analytics（読み取りのみ） |
| `storyboard` | 現行の `script` | `storyboard`（ADR-0015） | LLM（Codex CLI + OpenMontage 仕様、**有料**・予約台帳） |
| `production`（旧 `generation`） | 現行の `storyboard`, `script` | `scene_image` / `scene_voice` / `scene_video`, `production_manifest`（ADR-0017） | **有料** provider（画像・動画）/ ローカル TTS |
| `render` | 現行の `production_manifest`（と、それが指す `script` / `storyboard` / シーン素材） | `final_video`（ADR-0019） | なし（ローカル計算。CPU・ディスクを占有） |
| `upload` | 現行の `final_video`（と、メタデータを導出する `script`） | `upload_receipt`（ADR-0020） | **YouTube private 投稿**（予約台帳 `youtube_upload`、1ラウンドのみ） |
| `pipeline` | DB の Episode 状態・operational switch | `episodes` 行の claim（`topic_plan_id` を結ぶ） | なし（工程の起動は子 workflow） |
| `analytics`（**未実装**。`workers/analytics` は空） | `upload_receipt` | `performance_report`（予定） | YouTube Analytics。現状は Topic Planner が `analytics_snapshots` へ読み取るだけ |

- **持つ**: 「入力Artifactを読む → 処理する → 出力Artifactを書く → 結果を返す」
- **持たない**: 次のJobの決定（INV-4）、他workerのimport（INV-3）、
  Episodeの状態遷移の直接更新（遷移はdomainの規則を通す）
- 全Activityは冪等（INV-17）

## domain/

- **持つ**: `Episode` / `Job` / `Artifact` の型、状態値の列挙、遷移規則、
  失敗クラスの定義、不変条件を表す述語
- **持たない**: DB接続、HTTP、Temporal、ファイルI/O、時刻の直接取得（注入する）
- **呼んでよい**: `contracts` のみ
- 純粋関数の集合であること。これが守られている限り状態機械は全網羅テストできる

## contracts/

- **持つ**: Artifactのスキーマ定義（JSON Schema / Pydantic）、
  workflow-activity間のpayload型、共有される列挙と定数
- **持たない**: 振る舞い、外部依存
- 2箇所以上が一致していなければならない値は**ここにだけ**置く（AGENTS.md §8）

## infrastructure/

| モジュール | 責務 |
|---|---|
| `db/` | SQLAlchemy モデル、マイグレーション（Alembic）、リポジトリ実装 |
| `storage/` | MinIO クライアント、Artifactの put/get、キー規約 |
| `temporal/` | Temporal への接続（`connect.py`）、Daily Schedule の定義（`schedules.py`）、worker の health（`poller_check.py`）、実行の点検（`run_inspector.py`）。workflow 名と task queue 名は `contracts/`（`pipeline.py` / `states.py` / `topic_planning.py`） |
| `providers/` | fal.ai / YouTube / LLM の adapter。**必ずProtocolの背後に置く** |
| `observability/` | OpenTelemetry のtracer/meter設定、Prometheus exporter |

- worker の常駐は compose の1サービス1 worker（共通イメージ `worker` target）。起動は
  `infrastructure/runtime/worker_entry.py`、health は `infrastructure/temporal/poller_check.py`（ADR-0024）
- provider adapter は必ずfakeに差し替えられること（INV-18）
- secretはここから外へ出さない（INV-20）

## スケジューラ

Temporal Schedule として定義する。独立したcronプロセスを作らない（INV-2）。
