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
| [0027](./0027-daily-schedule-guard-and-watchdog.md) | Daily Schedule の望ましい状態・maintenance pause・watchdog | Accepted |
| [0028](./0028-voice-fit-before-render.md) | 音声の実尺を描画の前に区間へ合わせる | Accepted |
| [0029](./0029-topic-planner-activity-payload-shape.md) | Topic Planner の Activity 境界は型注釈どおりの形だけを返す | Accepted |
| [0030](./0030-provider-auth-failure-diagnostics-and-suppression.md) | provider 準備呼び出しの失敗診断・分類の是正と共有障害の抑止 | Accepted |
| [0031](./0031-daily-watchdog-progress-completion-upload.md) | Daily Watchdog を起動・進行・完成・投稿の4段階で判定する | Accepted |
| [0032](./0032-unified-episode-resume-entrypoint.md) | Episode の統一再開エントリポイント | Accepted |
| [0033](./0033-artifact-reuse-integrity-verification.md) | Artifact 再利用の完全性検証 | Accepted |
| [0034](./0034-provider-content-rejection-retry-and-prompt-mitigation.md) | provider のコンテンツ拒否は同じ入力で自動再送しない・拒否理由を全文残す・写実的な肖像を避ける | Accepted |
| [0035](./0035-scene-visual-subject-and-rejection-recovery.md) | シーンの映像対象を Storyboard で決め、内容拒否をシーン単位で復旧する | Accepted |
| [0036](./0036-research-search-provider-adapters.md) | 調査用の検索・本文取得を Port の背後の Adapter に閉じ、SSRF 規則を通してだけ取得する | Accepted |
| [0037](./0037-research-foundation-separate-persistence.md) | Research の依頼・外部呼び出し台帳・成果物を本番の表から分離して永続化する | Accepted |
| [0038](./0038-evidence-research-and-script-verification.md) | Evidence Research と台本の照合を research の成果物として実装する | Accepted |
| [0039](./0039-trend-research.md) | Trend Research を research の成果物として実装し、検証つきで読み出せるようにする | Accepted |
| [0040](./0040-structured-logs-and-opensearch-search-replica.md) | 構造化 JSON ログと OpenSearch の検索用副本（ログは業務判断に使わない） | Accepted |
| [0046](./0046-ci-minio-image.md) | CI の MinIO image を Chainguard（digest 固定）に差し替える | Accepted |
| [0049](./0049-branch-strategy-and-ci-gate.md) | ブランチ戦略（trunk-based・`main` = 本番）と単一の required check `CI gate` | Proposed（所有者承認待ち） |
| [0050](./0050-release-preflight-and-staging.md) | 読み取り専用の release preflight（READY/BLOCKED/UNKNOWN）と本番から隔離した staging | Proposed |
| [0051](./0051-restorable-backup-and-restore-drill.md) | 復旧できるバックアップ（pg_dump + S3 API 複写）と隔離先への restore drill | Proposed |
| [0053](./0053-unreconciled-reservation-reconciliation.md) | 未照合予約の照合記録（5値判定）・手動確定の監査・新 key での再送ガード | Proposed |
| [0052](./0052-temporal-container-init-and-leak-free-healthcheck.md) | Temporal コンテナを init 付きにし、healthcheck がプロセスを漏らさないようにする | Proposed |
| [0059](./0059-opensearch-memory-guard-and-single-container-recovery.md) | OpenSearch のメモリ保護（hysteresis）・単一コンテナの自動復旧（opt-in）・OpenSearch に依存しない警報と dead-man's switch（ADR-0040 §7 を更新） | Proposed |
| [0054](./0054-daily-workflow-failure-detection.md) | 日次 Workflow が失敗し Episode が作られない日を watchdog が検知（連続日数・unobservable・秘匿要約） | Proposed |
| [0055](./0055-destructive-schema-op-guard-and-db-role-separation.md) | 破壊的スキーマ操作の単一ガード（INV-69）と DB ロール分離の設計 | Proposed |
| [0057](./0057-fal-balance-exhaustion-managed-pause.md) | fal の残高不足を「管理された一時停止」として扱う（拒否の分類・永続 breaker・`blocked` + 保留・承認つき再開） | Proposed |
| [0058](./0058-daily-preflight-credit-hold-and-watchdog-classification.md) | 日次の事前診断（Codex model・breaker・保留数）・残高不足の保留・watchdog の失敗/保留/未納品の分類 | Proposed |
| [0060](./0060-check-vocabulary-widening-in-release-scan.md) | CHECK 語彙の拡張だけを、宣言と機械検査つきで release 走査の contract から外す | Proposed |
| [0061](./0061-fal-budget-zero-is-a-managed-hold.md) | fal の月間予算が 0 / 未設定でも、日次は失敗せず有料 submit を 0 回のまま正常終了する（ローカル予算ゲート。migration なし） | Proposed |
| [0062](./0062-release-only-reservation-waiver.md) | 未照合の provider 予約を、release に限り期限つき・1 件限りで waive する（台帳は書かない。migration なし） | Proposed |
| [0063](./0063-fal-monthly-hard-cap.md) | `FAL_MONTHLY_BUDGET_USD` を月間のハードキャップにする（予約と同じトランザクションで強制。migration なし） | Proposed |
| [0070](./0070-description-without-internal-ids.md) | YouTube description から内部 ID を外す（marker は tags に残す・既定 OFF の flag） | Accepted |
| [0071](./0071-geo-manifest-and-deterministic-map-verification.md) | 地図は GeoManifest から描き、位置の検証は決定論的な純関数が行う（LLM の採点で上書きしない） | Accepted |
| [0072](./0072-bgm-contracts-and-license-gated-selection.md) | BGM 契約とライセンスで門を閉じた選曲（契約・純粋ドメインのみ） | Proposed |
| [0073](./0073-quality-report-contract-and-fail-closed-gate.md) | 品質レポートの契約と fail-closed の投稿可否判定（契約層のみ・配線は後続） | Accepted |
| [0074](./0074-v3-phase2-shared-contract-fixes.md) | v3 Phase 2 の共有契約の是正（verdict 統一・入力保護・品質レポートの鮮度） | Accepted |
