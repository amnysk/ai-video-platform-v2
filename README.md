# ai-video-platform-v2

AIでYouTube動画を自動生成・投稿するプラットフォームの**第2世代基盤**。

前身の `ai-video-pipeline`（単一プロセスの状態機械 + SQLite）は、1工程の失敗が
工場全体を止め、途中再開の経路が工程ごとに散らばっていた。v2 はその再設計であり、
**部分的失敗が全体を止めない**ことを最上位の要件に置く。

## 現在のフェーズ

**Phase 0: 土台のみ。** 動画生成機能は未実装。このrepoにあるのは
ディレクトリ構成・契約文書・不変条件・CI設計だけ。
実装を始める前に [AGENTS.md](./AGENTS.md) を読むこと。

## 設計の柱

| 要件 | 実現手段 |
|---|---|
| 部分失敗で止まらない | Job単位の失敗分類 + Episodeはretryable失敗でterminalにしない |
| retry / 途中再開 | Temporal workflow のdurable execution + 冪等なActivity |
| 状態が明確 | Episode / Job / Artifact の3集約、PostgreSQLがsource of truth |
| Worker疎結合 | Workerは他Workerを知らない。Temporalだけが順序を決める |
| Web UIから操作 | FastAPIがcommandを受けてTemporalへsignal。UIはWorkerに触れない |
| AIエージェントが安全に開発 | 不変条件 + architecture test + ADR必須 |

## 技術スタック

Python 3.13 / FastAPI / Temporal OSS / PostgreSQL / MinIO /
Next.js + TypeScript / Docker Compose / pytest / Ruff / pyright /
GitHub Actions / OpenTelemetry / Prometheus / Grafana

## ドキュメント

- [AGENTS.md](./AGENTS.md) — 開発ルール（人間・AIエージェント共通）
- [docs/architecture/overview.md](./docs/architecture/overview.md)
- [docs/architecture/components.md](./docs/architecture/components.md)
- [docs/architecture/data-flow.md](./docs/architecture/data-flow.md)
- [docs/invariants.md](./docs/invariants.md) — **契約。ADRなしに変えない**
- [docs/failure-policy.md](./docs/failure-policy.md)
- [docs/domain/](./docs/domain/) — episode / job / artifact / state-transitions
- [docs/decisions/](./docs/decisions/) — ADR
- [docs/operations/legacy-asset-inventory.md](./docs/operations/legacy-asset-inventory.md) — 旧repo資産の棚卸し

## ディレクトリ

```text
apps/          FastAPI (apps/api) と Next.js (apps/web)。I/O境界のみ
workers/       Temporal worker。1工程1モジュール。相互import禁止
domain/        純粋なドメインモデルと状態遷移。I/O・框組みに依存しない
infrastructure/ DB・MinIO・Temporal client・provider adapter・計装
contracts/     Artifact schema と worker間のpayload契約（versioned）
docs/          設計・不変条件・ADR
tests/         unit / integration / contract / architecture
```

## 旧repoについて

`/home/yoshiki/projects/ai-video-pipeline` は**参照専用**。変更しない。
コードの移植は「そのまま」ではなくAdapter化を既定とする。判断は
[legacy-asset-inventory.md](./docs/operations/legacy-asset-inventory.md)。
