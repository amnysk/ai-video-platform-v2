# アーキテクチャ概要

## 解こうとしている問題

前身 `ai-video-pipeline` は、単一のワーカープロセスが状態機械を回し、
SQLiteの `jobs` テーブルを自分でleaseし、次の工程を自分で決めていた。
その結果:

- 1工程の失敗が状態機械を詰まらせ、工場全体が止まる
- 途中再開の経路が工程ごとに手書きされ、互いに食い違う
- 「次に何をするか」の知識がworkerの中に散り、変更のたびに壊れる
- プロセスがクラッシュすると、課金済みの外部呼び出しの照合が失われる

v2 は**実行の順序決定を Temporal に、状態の権威を PostgreSQL に**移し、
workerを「入力Artifactから出力Artifactを作るだけの関数」に縮める。

## 全体像

```text
        ┌──────────────┐
        │  Next.js UI  │  企画/制作/再実行/投稿/分析の操作
        └──────┬───────┘
               │ HTTP (only path in)                      INV-1
        ┌──────▼───────┐
        │   FastAPI    │  検証 → DB書き込み → workflow start/signal
        │  (apps/api)  │  重い処理は一切しない                INV-16
        └──┬────────┬──┘
           │        │ start / signal / query
   ┌───────▼──┐  ┌──▼─────────────┐
   │PostgreSQL│  │  Temporal OSS  │  順序・retry・timeout・補償  INV-5
   │ (source  │  │                │
   │ of truth)│  └──┬─────────────┘
   │  INV-7   │     │ dispatch activity (task queue)
   └───▲──────┘     │
       │        ┌───▼──────────────────────────────┐
       │        │ Workers (workers/*)              │
       │        │ planning / generation / render / │  互いを知らない INV-3
       │        │ upload / analytics               │  次を決めない   INV-4
       │        └───┬──────────────────────────────┘
       │            │ read/write artifacts
       │        ┌───▼────┐
       └────────┤ MinIO  │  Artifact本体  INV-9
   参照のみ保存  └────────┘
```

Scheduler（定期企画など）は Temporal Schedule として表現し、
workerを直接叩かない（INV-2）。

## レイヤ

| レイヤ | ディレクトリ | 責務 | 依存してよい先 |
|---|---|---|---|
| Presentation | `apps/web` | 操作UI | HTTP API のみ |
| Application | `apps/api` | command受付・検証・workflow起動 | domain, contracts, infrastructure |
| Orchestration | Temporal workflow定義（`workers/*/workflows.py`） | 工程順序・retry方針 | domain, contracts |
| Execution | `workers/*/activities.py` | 1工程の実行 | domain, contracts, infrastructure |
| Domain | `domain/` | Episode/Job/Artifactのモデルと遷移規則 | contracts のみ |
| Contracts | `contracts/` | Artifactスキーマ・payload定義 | なし |
| Infrastructure | `infrastructure/` | DB / MinIO / Temporal client / provider adapter / 計装 | domain, contracts（ADR-0007） |

依存は上から下への一方向のみ（INV-6）。

## なぜこの分割か

- **domainがI/Oを知らない**ので、状態遷移規則を実DBなしで全網羅テストできる。
  前身repoで状態機械のテストが重すぎて書かれなかった問題への対処。
- **workerが次を決めない**ので、工程を1つ足すときに触るのはworkflow定義1箇所。
  前身repoでは工程追加のたびにworkerとautomationの両方を直す必要があり、
  片方だけ直す事故が繰り返された。
- **Artifactがimmutableでversion付き**なので、再開判定が「出力が既にあるか」
  という1つの述語に集約される。工程ごとの再開フラグが不要になる。

## Temporal と domain state の分離

Temporalは「その実行がどこまで進んだか」を持ち、PostgreSQLは
「このEpisodeは業務上どういう状態か」を持つ。両者は対応しない（INV-8）。

- workflowがcompletedでも、Episodeは `needs_review`（人間待ち）でありうる
- workflowがterminatedでも、Episodeは `blocked` であって `failed` ではない
- UIに出すのは常にdomain state

## 現在の実装状況

**Phase 1（最小の縦切り）**。上の箱のうち、UI以外は骨組みが通っている:
FastAPI が Episode を作って workflow を start し、
`EpisodeSkeletonWorkflow` が dummy Activity を1つ実行し、
Artifact を MinIO へ、メタデータを PostgreSQL へ書き、Episode を `completed` にする。

まだ無いもの: Next.js UI、企画/台本/素材生成/レンダー/投稿/分析の各worker、
有料provider adapter、OpenTelemetry の実配線。
