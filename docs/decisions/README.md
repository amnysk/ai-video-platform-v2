# Architecture Decision Records

architecture / invariant / contract に触れる変更は、コードより先にADRを書く
（[AGENTS.md §6](../../AGENTS.md)）。

## 書式

ファイル名: `NNNN-kebab-case-title.md`（連番、再利用しない）

必須節（順序固定）:

1. **Status** — `Proposed` / `Accepted` / `Superseded by ADR-NNNN` / `Deprecated`
2. **Context** — 何が問題か。実データ・実際に起きた事故を書く
3. **Decision** — 何を決めたか。1文で言い切る
4. **Alternatives** — 検討して**採らなかった**案と、採らなかった理由
5. **Consequences** — 良い結果・悪い結果の両方。特に**引き受けた負債**を明記

Alternatives と Consequences の悪い側を書いていないADRは未完成として扱う。

新しいADRを足したら**この一覧にも同じコミットで追記する**
（検査: `tests/architecture/test_docs_contract.py::test_every_adr_is_listed_in_the_index`）。

## 一覧

| ADR | タイトル | Status |
|---|---|---|
| [0001](./0001-adopt-temporal.md) | Temporalを採用する | Accepted |
| [0002](./0002-adopt-postgresql.md) | PostgreSQLをsource of truthにする | Accepted |
| [0003](./0003-adopt-minio.md) | MinIOでArtifact本体を保持する | Accepted |
| [0004](./0004-adopt-fastapi.md) | FastAPIをAPI層に採用する | Accepted |
| [0005](./0005-adopt-docker-compose.md) | Docker Composeでローカル環境を組む | Accepted |
| [0006](./0006-skeleton-episode-state-subset.md) | 骨組み用のEpisode `completed` とJob状態語彙 | Accepted |
| [0007](./0007-infrastructure-may-depend-on-domain.md) | infrastructure → domain の依存を許可 | Accepted |
| [0008](./0008-adopt-psycopg3-as-the-only-postgres-driver.md) | PostgreSQLドライバを psycopg v3 に一本化 | Accepted |
| [0009](./0009-python-dependency-single-source.md) | Python依存の宣言元を pyproject.toml ひとつに | Accepted |
| [0010](./0010-phase1-artifact-versioning-deferral.md) | INV-10 の `version` と INV-14 を Phase 2 発効に | Accepted（0012 が version 部分を発効） |
| [0011](./0011-episode-script-ready-state.md) | Episode に `script_ready` を導入 | Accepted |
| [0012](./0012-artifact-identity-for-nondeterministic-generators.md) | Artifact の同一性を input_hash へ移す | Accepted |
| [0013](./0013-provider-reservation-ledger.md) | 外部AI呼び出しの予約台帳と INV-15 の意味論 | Accepted |
| [0014](./0014-llm-output-defects-are-retryable.md) | LLM出力の形式不正は retryable | Accepted |
| [0015](./0015-storyboard-stage.md) | Storyboard 工程と Episode `storyboard_ready` | Accepted |
| [0016](./0016-openmontage-guided-storyboard-generation.md) | Storyboard 生成は固定した OpenMontage 仕様で誘導した Codex | Accepted |
| [0017](./0017-production-stage.md) | Production 工程（画像・音声・動画）と Episode `assets_ready` | Accepted |
| [0018](./0018-scene-scoped-artifacts.md) | シーン単位の Artifact・job・予約 | Accepted |
| [0019](./0019-render-stage.md) | Render 工程（完成動画）と Episode `render_ready` | Accepted |
| [0020](./0020-upload-stage.md) | Upload 工程（YouTube private 投稿）と Episode `uploaded` | Accepted |
| [0021](./0021-operational-switches-daily-slots-and-test-db-isolation.md) | 運用スイッチ・日次 Episode 枠とテストDBの隔離 | Accepted |
| [0022](./0022-youtube-processing-check.md) | 投稿後の YouTube 処理状態の確認と送信中の一時停止 | Accepted |
| [0023](./0023-daily-schedule-and-episode-pipeline.md) | Daily Schedule と Episode pipeline workflow | Accepted |
| [0024](./0024-compose-managed-workers.md) | 常駐 Worker を Docker Compose で管理する | Accepted |
| [0025](./0025-topic-planner.md) | Topic Planner（TopicPlan を確定してから Episode を作る） | Accepted |
| [0026](./0026-script-locale.md) | 台本の locale は Strategy profile が決め、Topic Plan の題材を明示的に渡す | Accepted |
| [0027](./0027-voice-fit-before-render.md) | 音声の実尺を描画の前に区間へ合わせる | Accepted |
