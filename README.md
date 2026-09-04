# ai-video-platform-v2

AIでYouTube動画を自動生成・投稿するプラットフォームの**第2世代基盤**。

前身の `ai-video-pipeline`（単一プロセスの状態機械 + SQLite）は、1工程の失敗が
工場全体を止め、途中再開の経路が工程ごとに散らばっていた。v2 はその再設計であり、
**部分的失敗が全体を止めない**ことを最上位の要件に置く。

## 現在のフェーズ

**Phase 1: 最小の縦切り。** 1つのEpisodeが Temporal / PostgreSQL / MinIO を
使って安全に状態遷移するところまで通っている。
**動画生成・YouTube投稿・Storyboard・Production は未実装**（Phase 2以降）。

実装を始める前に [AGENTS.md](./AGENTS.md) を読むこと。

### 動く経路

```text
POST /episodes → episodes(planned) → EpisodeSkeletonWorkflow start
  → mark_episode_in_progress   : episodes(in_progress)
  → create_dummy_job           : jobs(queued)
  → produce_dummy_artifact     : jobs(running) → MinIOへdummy JSON
                                 → artifact_metadata(sha256) → jobs(succeeded)
  → complete_episode           : episodes(completed)
GET /episodes/{id} → Episode + Jobs + Artifact metadata
```

### 動かす

```bash
cp .env.example .env            # secretはここに。コミットしない
docker compose --profile core up -d --wait
./scripts/smoke.sh              # Episodeを1本流して completed を確認
```

- API: http://localhost:8000/docs
- Temporal UI: http://localhost:8233
- MinIO console: http://localhost:9001（実データは `/mnt/minio-hdd/minio-data`）

### テスト

```bash
python -m venv .venv && .venv/bin/pip install -e ".[dev]"
.venv/bin/pytest tests/unit tests/contract tests/architecture   # Docker不要
.venv/bin/pytest tests/integration -m integration               # 要 docker compose
.venv/bin/ruff check . && .venv/bin/pyright
```

`tests/integration/test_episode_workflow.py` は Temporal の time-skipping
テスト環境を使うので Docker なしでも走る（実PG・実MinIOのテストだけがskipされる）。

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
apps/api       FastAPI。検証 → 永続化 → workflow起動だけ（INV-16）
apps/web       Next.js（未実装）
workers/dummy  Temporal worker（骨組み）。workflows.py が順序を持つ唯一の場所
domain/        純粋なドメインモデルと状態遷移。I/O・フレームワークに依存しない
infrastructure/ DB・MinIO・Temporal client・provider adapter・計装
contracts/     状態値の語彙(states.py)とArtifactスキーマ(artifacts.py)。定義はここに1つだけ
docs/          設計・不変条件・ADR
tests/         unit / integration / contract / architecture
```

## 次に実装すべきこと（Phase 2 の入口）

1. `contracts/schemas/` に本番Artifactのスキーマ（`episode_plan` / `script`）
2. Artifact の `version` 列と `superseded` 状態（INV-10 / INV-11 の残り）
3. `input_hash` による工程skip（failure-policy §4 の「途中再開」）
4. OpenTelemetry の実配線と Prometheus メトリクス
5. Next.js UI（一覧・詳細・再実行）
6. 旧repoからの移植: `youtube_uploader/` → upload worker、fal adapter → generation worker

## 旧repoについて

`/home/yoshiki/projects/ai-video-pipeline` は**参照専用**。変更しない。
コードの移植は「そのまま」ではなくAdapter化を既定とする。判断は
[legacy-asset-inventory.md](./docs/operations/legacy-asset-inventory.md)。
