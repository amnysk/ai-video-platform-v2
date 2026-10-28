# v3 Phase 2 前監査: 本番差分・意味的マージ・QualityGate 配線の実現性

- 作成日: 2026-10-29
- 状態: **監査（docs のみ。コード変更なし）**。基準ツリー: `claude/v3-phase2-base`（6d14c30）
- 監査方法: 6d14c30 のファイル読み取りと `git diff` / `git log` のみ。本番 DB・Temporal・Docker・YouTube・fal・ネットワークには触れていない。
- **本番の live 状態は 未確認（結果待ち）**。§6 のコマンドをオーナーが本番ホストで実行した結果で埋める。推測で埋めない。
- 前提: [v3-phase0-audit.md](./v3-phase0-audit.md) のリスク R9（2026-10-27 以降の本番 SHA が未検証）。

## 1. SHA の系譜

| SHA | 内容 | 日付 | 関係 |
|---|---|---|---|
| 80775c0 | `claude/daily-hardening`（PR #2 opensearch-logging のマージ）。Wave 1 の元の土台 | 2026-10-07 | 1227e26 の祖先 |
| 1227e26 | 記録済みの 1 つ前の本番 release | 2026-10-26 | 80775c0 の子孫、303f33e の祖先 |
| 303f33e | **最後に記録された本番 release**（PR #19 fal-monthly-cap。`claude/release-record-cap` の release record） | 2026-10-27 | 80775c0 の子孫 |
| acb4bb0 | Wave 1 integration（`claude/v3-wave1-integration`）。**80775c0 の上**に作られた | 2026-10-29 | 303f33e を含まない |
| 6d14c30 | `claude/v3-phase2-base` = acb4bb0 を 303f33e にマージ | 2026-10-29 | 第1親 303f33e / 第2親 acb4bb0 |

`git merge-base --is-ancestor 80775c0 303f33e` は成立（80775c0 ⊂ 1227e26 ⊂ 303f33e）。
80775c0..303f33e は 122 commit・295 ファイル（+35083 / -216）。

### 1.1 acb4bb0 を 80775c0 のまま本番へ出していたら失われたもの

acb4bb0 は 303f33e を含まないので、そのまま deploy すると本番の次が消える（コード・テスト・設計書とも）。

| 区分 | 失われるもの |
|---|---|
| ADR | 0046 / 0049 / 0050 / 0051 / 0052 / 0053 / 0054 / 0055 / 0057 / 0058 / 0059 / 0060 / 0061 / 0062 / 0063（0040 の改訂も）。差分に 0056 は現れない |
| INV | INV-65〜69, 71〜86（差分に INV-70 は現れない）。うち本番の安全に直結: INV-67 未照合予約（ADR-0053）、INV-73 残高不足の保留（ADR-0057）、INV-84 周辺の fal 予算・月間ハードキャップ（ADR-0061 / 0063）、INV-65 release preflight（ADR-0050） |
| migration | 0016 `provider_reservation_reconciliations`、0017 `daily_workflow_failure_kinds`、0018 `provider_breaker_and_credit_holds`（alembic head 0018 → 0015 に後退）。**DB が 0018 のまま旧コードを動かすと `alembic upgrade head` が失敗する**（`domain/release_preflight.py` の `_check_db_ahead` が検出する対象） |
| scripts | `release-preflight.py`、`reconcile-reservations.py`、`schedule-guard.py` の維持機能、`provider-credits.py` など |

結論: Phase 2 の土台は 6d14c30（303f33e 上）でなければならない。80775c0 上の acb4bb0 を deploy 対象にしてはならない。

## 2. 意味的マージ監査

テキスト競合は docs/decisions/README.md と docs/invariants.md のみだった（親が解消済み）。以下は、テキストでは検出できない相互作用の監査。
Wave 1 の変更ファイルは `contracts/{upload,geo,music,quality,agent_tool_policy}.py`、`domain/{geo,music,quality}/`、`infrastructure/youtube/metadata.py`。
`contracts/states.py`・`contracts/pipeline.py`・`contracts/log_contract.py`・`workers/`・`infrastructure/db/`・migration・`pyproject.toml`・`constraints.txt`・`Dockerfile`・`compose*.yaml` は **Wave 1 で変更なし**（`git diff 303f33e 6d14c30` に現れない）。

| # | 観点 | 結論 | 根拠（6d14c30 の file:line） |
|---|---|---|---|
| 1 | 投稿冪等キー（ADR-0013/0020） | SAFE | `compute_upload_key` は episode・final_video の sha256・投稿先だけから決まる（`domain/upload/keys.py:15`）。Wave 1 の flag・lint は description の組み立てだけで、キーに入らない。`workers/upload/activities.py:432-436` |
| 2 | `_reserve` / 予約台帳（ADR-0053） | SAFE | `_reserve`（`workers/upload/activities.py:528`）と `_drive`（`:639`）・台帳は Wave 1 で未変更。`build_youtube_metadata` は予約より前（`:439`）に純粋に呼ばれるだけ |
| 3 | flag 既定 OFF の byte 同一性 | SAFE | `contracts/upload.py:118-121` は `1` / `true` 以外 OFF。`workers/upload/activities.py:439-445` は `required_parts` を渡さず、OFF では従来どおり `本文 + "\n\n" + marker`（`contracts/upload.py:261-264`）。compose にも flag なし（`grep` 0 件） |
| 3b | flag ON 時の marker 照合 | NEEDS-ATTENTION（ON にする前に） | 受領の検証は `metadata.tags` に marker が入ることだけを要求（`contracts/upload.py:305`）。`find_video_by_marker` はタグ**または** description 行で一致（`infrastructure/youtube/uploader.py:392-438`）。flag ON では **tags が唯一の照合手段**になり、視聴者・YouTube 側のタグ編集で二重投稿防止が弱まる。ADR-0070 §5.2 / INV-91 は認識済み。ON は owner の明示判断（AGENTS.md §9）で、Phase 2 のデプロイでは ON にしない |
| 3c | flag 切替と再試行 | NEEDS-ATTENTION | `build_youtube_metadata` は試行ごとに呼ばれる（`workers/upload/activities.py:439`）。稼働中に flag を切り替えると、送信前の metadata snapshot と description が食い違う（ADR-0070 の「悪い点」）。切替は pause 中（UPLOADS_PAUSED）に限る運用を release 手順へ書く |
| 4 | description lint の誤検出 | NEEDS-ATTENTION（flag ON のみ） | `INTERNAL_ID_PATTERNS`（`contracts/upload.py:87-99`）は 32 桁以上の hex・UUID・`artifacts/…/<sha>` を拒否。誤検出は `InternalIdInDescriptionError` → 投稿 activity 失敗（保留側）。flag OFF では lint は `build_youtube_metadata` も `insert_body` も実行されない（`contracts/upload.py:266`、`infrastructure/youtube/metadata.py:24-26`）ので本番の現行挙動は変わらない |
| 5 | PipelineStage / EpisodeStatus / ArtifactType の値 | SAFE | Wave 1 は列挙値を足していない。`contracts/states.py:12-30`（EpisodeStatus）・`:102-118`（ArtifactType）は 303f33e と同一。`domain/quality/outcome.py` は既存 `EpisodeStatus` を**参照するだけ** |
| 6 | `needs_input` 保留（ADR-0057/0058/0061/0063） | SAFE | `provider_credit_holds`（`infrastructure/db/models.py:497-537`）・`HoldReason`・`_hold_for_budget`（`workers/pipeline/activities.py:300-343`）に Wave 1 は触れない。QualityReason は別の列挙で、`HoldReason` と値を共有しない |
| 7 | fal 予算・月間キャップ（paid_job.py） | SAFE | `infrastructure/production/paid_job.py` は未変更。Wave 1 に fal / provider 呼び出しは無い（geo・music・quality は純粋 domain。`tests/architecture/test_{geo,music,quality_gate}_boundaries.py` が I/O 禁止を強制） |
| 8 | provider_reservations の照合 | SAFE | `ProviderCall` / `ReservationStatus` / `ReconciliationVerdict`（`contracts/states.py:203-250`）未変更。Wave 1 は予約を読み書きしない |
| 9 | release preflight / migration scan（ADR-0050/0060） | SAFE | Wave 1 に migration が無いので scan の対象外（alembic head は 0018 のまま）。ただし **対象 SHA ごとの replay 証跡** `release/evidence/<sha>-replay.json`（`domain/release_preflight.py:1351-1372`）と承認ファイルは 6d14c30（またはその子孫）用に**新規作成が必要**。303f33e 用のものは流用不可（`doc["sha"]` 不一致で BLOCKED）。`release/` ディレクトリはこのツリーに無い（ここでは手元に何もない） |
| 10 | ログ語彙（`contracts/log_contract.py`） | SAFE | Wave 1 は EventName・理由を足していない。lint の例外文は内部 ID の**値を出さない**（`contracts/upload.py:132-137`）。QualityGate 配線時に新しい event を足すなら log contract の追加が要る（§3） |
| 11 | 依存の単一定義（ADR-0009） | SAFE | `pyproject.toml` / `constraints.txt` / `Dockerfile` に差分なし |
| 12 | INV / ADR 番号 | SAFE | 本番側は INV-65〜86・ADR-0063 まで、Wave 1 は INV-90〜104・ADR-0070〜0073。INV-87〜89 は欠番（予約帯 INV-41〜89。`docs/invariants.md` §AB 冒頭）。0064〜0069 は使わない。`test_docs_contract` は 6d14c30 で緑（親が確認） |
| 13 | `workflow` の replay | SAFE | Wave 1 は workflow・Activity・その入出力 dataclass を変更しない。記録済み履歴の replay テストは `tests/unit/test_daily_watchdog_replay.py` と `tests/unit/test_log_old_history_replay.py`（`EpisodePipelineWorkflow` の履歴 fixture は無い。`docs/operations/release-runbook.md` の 184 行目に既知の穴として記載） |

**BLOCKER: なし。** NEEDS-ATTENTION は 3b / 3c / 4 と、§3 の配線時の論点。いずれも flag ON にしない限り本番の挙動は変わらない（既定 OFF）。

## 3. QualityGate 配線（Phase 2-C）の実現性

### 3.1 現状

- `upload_gate`（`workers/pipeline/activities.py:343-351`）→ `_refusal(session, ep, exists, status)`（`:353-374`）。
  確認するのは: Episode の存在、`status == render_ready`、現行 `final_video` Artifact、`youtube_upload` 予約が SPENT / dispatched でないこと。品質レポートは見ない。
- 拒否は例外ではなく `UploadGateResult(allowed=False, reason=<str>, status=<str>)`（`contracts/pipeline.py:308-313`）。
- 呼び出し側は `EpisodePipelineWorkflow`（`workers/pipeline/workflows.py:405-428`）の `_state_activity(PIPELINE_UPLOAD_GATE, UploadGateRequest(episode_id=ep), UploadGateResult)`。
  拒否なら `UPLOAD_SKIPPED` event（`contracts/log_contract.py:380`）を出し、`PipelineOutcome.UPLOAD_SKIPPED`（`contracts/pipeline.py:145`）で**正常終了**する。Episode の状態は変えない（`render_ready` のまま）。

### 3.2 回答

**Q1. 新しい PipelineStage / EpisodeStatus / Activity / 入出力 dataclass の変更なしで、`_refusal()` に品質レポートの検査を足せるか。**
**可能（yes）。**
- Activity の本体（`_refusal`）は replay されない。Temporal の replay が再実行するのは workflow コードで、Activity の結果は履歴から読まれる。したがって `_refusal` の判定ロジックの追加は、進行中の workflow の履歴に対して非決定を起こさない。
- `UploadGateRequest` / `UploadGateResult` のフィールドはそのまま使える。品質レポート由来の拒否は `reason: str`（`QualityReason` の値を含む文字列）に載せる。`status` は `render_ready` のまま。
- workflow の分岐（`not gate.allowed` → UPLOAD_SKIPPED）も変わらない。
- 守ること: (a) `_refusal` に引数・戻り値を足さず、レポートの取得は `self._session_factory` と既存 repository の範囲で行う。(b) `reason` を機械処理する箇所は無い（`attributes={"reason": gate.reason}` に入るだけ）。ただし、`reason` の語彙を log contract に足すなら `contracts/log_contract.py` を同じコミットで更新する。(c) 既存テスト `tests/unit/test_pipeline_activities.py:225-305`・`test_pipeline_workflows.py:512-575` は既定でレポート無しの Episode を通すので、**そのままではレポート無し＝拒否に変わり落ちる**。INV-101（無い＝PASS ではない）と整合させるため、テストの変更は ADR で先に承認する（AGENTS.md §2）。

**Q2. REVIEW（人の承認待ち）は既存のどの拒否・保留に対応するか。**
- `domain/quality/outcome.py` は REVIEW → `EpisodeStatus.BLOCKED`、BLOCK → `NEEDS_WORK` と書いている。**しかし `render_ready → blocked` / `render_ready → needs_work` は遷移表（`docs/domain/state-transitions.md`、`domain/episode/transitions.py`）に無く**、`render_ready` からの遷移は `STAGE_ADMITTED`（→ `in_progress`）だけ（`domain/episode/transitions.py:71`）。よって状態遷移を伴う配線は遷移表の変更＝ADR が必要。
- **遷移なしで成立する写像（推奨）**: REVIEW も BLOCK も `upload_gate` が `allowed=False` を返し、Episode は `render_ready` のまま。`UPLOAD_SKIPPED` で日次は正常終了し、`render_ready` の出口（人による再描画 / upload POST。`docs/domain/state-transitions.md` 出口の保証）と watchdog の投稿期限検知（`infrastructure/temporal/watchdog.py:384-430` の `EPISODE_NOT_UPLOADED_BY_DEADLINE`）が通報を担う。既存の `needs_input` 保留（`provider_credit_holds`）は provider の残高不足専用（`HoldReason` の CHECK。`infrastructure/db/models.py:507`）で、品質保留に流用しない（流用は列挙の拡張＝DDL）。
- **未解決の穴（NEEDS-ATTENTION）**: 手動の `POST /episodes/{id}/upload`（`apps/api/routers/episodes.py:236-285`）は `UploadWorkflow` を直接起動し、**`upload_gate` を通らない**。`workers/upload/activities.py` の入場も品質レポートを見ない。`_refusal` だけに配線すると、人が REVIEW の Episode を承認する手段（= upload POST）が品質検査そのものを素通りし、REVIEW / BLOCK のレポートでも投稿できてしまう。INV-104 の文言（`upload_gate` は…拒否する）は自動パイプライン経路だけを覆う。手動経路をどうするか（API の 409、または upload worker 入場での検査、または承認 token の導入）は ADR で決める。
- 人の承認の記録先: 既存の仕組みには「REVIEW を承認した」を記録する場所が無い。承認を `UploadGateResult` で運ぶことはできない。承認の保存は §3.3 と同じく ADR 事項。

**Q3. QualityReport の保存・読み出し先（DDL なしで可能か）。**
- `artifact_metadata.artifact_type` は DB の CHECK（`ck_artifact_metadata_type`、`infrastructure/db/models.py:183`、`_check("artifact_type", ArtifactType, ...)`）で `ArtifactType` の値に縛られている。**新しい Artifact 種別（`quality_report`）の追加は CHECK の widening の migration（0019 以降）が要り、DDL**。ADR-0055 / ADR-0060 の走査対象になる。
- DDL なしで可能な範囲: (a) 既存の `PRODUCTION_MANIFEST` / `UPLOAD_RECEIPT` などにレポートを相乗りさせる — 意味が混ざり content-addressing（`domain/artifact/keys.py:25-37` の `artifacts/{episode}/{type}/{sha}.json`）と supersede 規則（種別ごとに current が1本）を壊すので**非推奨**。(b) MinIO に決定論のキー（例 `artifacts/{episode}/quality_report/{final_video_sha256}.json`）で置き、行を持たない — 読み書きは `domain/artifact/keys.py` に 1 関数足すだけで DDL 不要だが、Artifact の台帳（artifact_metadata の行）の外に出るため、整合・消去・監査の規則を ADR で定める必要がある。
- 読み出しは `_refusal` 内で `ArtifactMetadataRepository` か MinIO の読み取りを使う。`upload_gate` Activity のタイムアウト・リトライ設定は変えない（MinIO 読み取り失敗は INV-101 により**拒否**側に倒す）。
- 結論: **正規の保存先（Artifact 種別 + metadata 行）には DDL が要る**。DDL なしの暫定は (b) で、可能だが ADR が要る。

### 3.3 ADR が必要なもの（配線の前に承認）

1. 品質レポートの保存先: 新 `ArtifactType` + migration（CHECK widening、ADR-0055/0060 の scan 対象）か、MinIO 直置き（台帳の外）か。
2. `render_ready` の遷移表: REVIEW / BLOCK を状態遷移にするか（`render_ready → blocked` / `→ needs_work`）、状態不変で `allowed=False` にするか。後者なら `domain/quality/outcome.py` の `episode_status_for` との食い違い（同じ語彙が2箇所）を整理する（AGENTS.md §8）。
3. 手動 upload POST の経路（`apps/api/routers/episodes.py:236`）と upload worker 入場に品質検査を置くか。INV-104 の適用範囲。
4. 「レポート無し＝拒否」を既定にすることによる既存テストの変更（AGENTS.md §2。仕様変更として ADR で先に承認）と、移行期の挙動（flag か、全 Episode にレポート必須か）。flag を足す場合は `workflow` 入力に載せない（履歴の形を変えない）。
5. REVIEW の人による承認の記録（誰が・何を・いつ承認したか）の保存先と、承認が final_video の sha256 に結び付くこと。
6. `reason` の語彙（`QualityReason`）を log contract に写すか。
7. レポートを**生成**する側（検査の実行主体）。本監査の範囲外だが、生成が Activity なら `contracts/pipeline.py` に名前を足す＝Activity の追加で、replay 証跡の更新（`release/evidence/<sha>-replay.json`）が要る。

## 4. Phase 2 を止める条件のまとめ

- 本番 SHA が 303f33e でない・祖先関係が崩れている・alembic が 0018 でない → Phase 2 の deploy 準備を止め、差分を再監査する（§6）。
- 稼働中 workflow がある・未照合の provider_reservations がある → deploy 不可（ADR-0050 / ADR-0053。waiver は ADR-0062 の手順）。
- flag `AVP_UPLOAD_DESCRIPTION_WITHOUT_MARKER` は Phase 2 の deploy で ON にしない。
- QualityGate の配線は §3.3 の ADR 承認まで着手しない。

## 5. 本番の live 状態

**実測済み（2026-10-28 16:38 UTC、親エージェント。すべて読み取りのみ: `docker ps/inspect`、
`scripts/workers-versions.sh`、`BEGIN READ ONLY` 内の SELECT、`temporal schedule list` / `workflow list`、
`printenv` の予算・flag キーのみ）。** 何も変更していない。

| 項目 | 値 | 判定 |
|---|---|---|
| 稼働中の SHA（全14アプリコンテナの image label） | `303f33ec7d0430009bd7d25e15e7bfbd813971d9`。1種類・STALE なし | Phase 2 base（6d14c30）の親。巻き戻しなし |
| コンテナ作成時刻 | 2026-10-27T14:31:38Z（リリース記録と一致） | 記録外のデプロイなし |
| alembic current | `0018` | head と一致（Phase 2 は migration なし） |
| Temporal | `SERVING` | — |
| schedule | `avp-daily-episode` paused=false（次回 約4時間後）、`avp-daily-watchdog` paused=false | — |
| 稼働中 workflow | 0 件 | — |
| 未照合の provider_reservations（status=reserved） | 0 件（全 426 行が spent。youtube_upload 16 行も spent） | — |
| open な credit hold | 1 件（`waiting_for_credits`、resumable_stage=production、2026-10-27 21:21Z） | リリース記録の予告どおり月間上限で保留 |
| Episode | uploaded 16 / completed 23 / blocked 16 / render_ready 1 / planned 1 / cancelled 1 | — |
| `FAL_MONTHLY_BUDGET_USD`（production 系 worker） | **60**（0 ではない。ADR-0063 のハードキャップ、2026-10-27 所有者承認） | 「予算0を維持」という前提と不一致 |
| `AVP_UPLOAD_DESCRIPTION_WITHOUT_MARKER` ほか v3 flag | どの worker の環境にも未設定（= 既定 OFF） | — |
| OpenSearch | `avp2-logging-opensearch-1` が 8 日前から Exited(143)。`avp-logging-check.service` が failed | デプロイ後のログ検証ができない |

## 6. オーナー用 read-only チェックリスト

本番ホストで、repo のルートから実行する。**すべて読み取り専用**（各コマンドの根拠を併記）。
`docker` / `make up` / `alembic upgrade` / deploy / `schedule-guard.py maintenance …` / `reconcile-reservations.py reconcile --apply` / `resolve` は**実行しない**。

| # | コマンド | 読み取り専用である根拠 | 期待値 | Phase 2 を止める結果 |
|---|---|---|---|---|
| 1 | `git rev-parse HEAD` | git の参照読み取り | 303f33e（フル SHA は `git rev-parse 303f33e`）。release record は `claude/release-record-cap` の docs/releases/2026-10-27-fal-monthly-cap.md | 303f33e の子孫でない／祖先が 1227e26 でない。`git merge-base --is-ancestor 303f33e HEAD` が失敗なら止める |
| 2 | `git branch -a --contains 1227e26` | 同上 | 本番 HEAD のブランチが含まれる | 1227e26 を含まない |
| 3 | `./scripts/workers-versions.sh` | スクリプト冒頭に「docker の読み取りだけで、何も変更しない」（`scripts/workers-versions.sh:1-9`）。`docker compose ps` / `docker inspect` のみ。**この実行は docker を使うのでオーナーが行う**（エージェントは実行しない） | 全アプリコンテナが現在のイメージで、revision が 1 種類、`EXPECTED_REVISION=<303f33e のフル SHA>` を付けて exit 0 | STALE / NO-REVISION / revision が 2 種類以上 / 期待と不一致（exit 1） |
| 4 | `alembic current`（`alembic.ini:2` の script_location = `infrastructure/db/migrations`。env は `Settings` から DB を解決） | `current` は `alembic_version` の読み取りだけ。`upgrade` / `stamp` は使わない | `0018`（head と一致。`infrastructure/db/migrations/versions/0018_provider_breaker_and_credit_holds.py`） | 0018 でない（0015〜0017 なら本番は 303f33e でない。0018 より新しいなら Phase 2 の土台を再確認） |
| 5 | `python scripts/schedule-guard.py status --json` | `status` は `describe` の読み取りと表示だけ（`scripts/schedule-guard.py` の `_status`）。`maintenance begin/end` と `reconcile` は使わない。終了コード 0 / 1 / 3 | daily・watchdog とも存在。運用者の pause の有無・ガードの印を記録（値は未確認） | exit 1（describe できない）・exit 3（運用者の pause）・watchdog が pause（自動化が無監視）・ガードの印が残る古い maintenance pause |
| 6 | `temporal workflow list --query 'ExecutionStatus="Running"'` | list は読み取り。`temporal workflow terminate/signal/cancel` は使わない。同じ件数は `release-preflight.py` の `running_workflow_count`（`infrastructure/release/collectors.py:303`）も読み取りで数える | 0 件（deploy は無停止の前提ではない）。残る場合は workflow 種別・ID を記録 | 1 件以上の `EpisodePipelineWorkflow` / `UploadWorkflow` / `ProductionWorkflow`（特に Upload が送信中）。release-runbook の手順で drain するまで deploy 不可 |
| 7 | `python scripts/reconcile-reservations.py list` | docstring に「読み取り専用（DB 側も READ ONLY transaction）」（`scripts/reconcile-reservations.py:9`、`_read_only` `:94-96`）。`reconcile`（`--apply`）と `resolve` は使わない。provider に接続しない | 未照合 0 件（出力は JSON） | 1 件以上（ADR-0053 / INV-67。ADR-0062 の waiver 無しでは deploy 不可）。特に `youtube_upload` の dispatched で video id が無い行 |
| 8 | `python scripts/operational-switch.py show` | `show` は表示のみ（`set` は使わない）。`scripts/operational-switch.py:1-9` | `paused` / `uploads_paused` の現状値を記録（値は未確認） | 意図しない paused / uploads_paused が残っている |
| 9 | `python scripts/release-preflight.py --target-sha <6d14c30 のフル SHA> --backup-manifest <PATH> --json` | docstring に「読み取り専用の release preflight」「何も変更しない」（`scripts/release-preflight.py:2-9`）。READY=0 / BLOCKED=2 / UNKNOWN=3 | **最終確認用**。承認・replay 証跡が無い段階では UNKNOWN（`release/evidence/<sha>-replay.json` 等が必要、§2 #9）。ここでは `ancestry`・`alembic`・`reservations` の各行を確認 | BLOCKED の行（特に `ancestry` が fast-forward でない／`alembic` が 0018 でない／未照合予約）。UNKNOWN は READY とみなさない |

注意:
- #3 と #9 の一部は docker / Temporal / DB を読むので、実行はオーナーの判断で行う。エージェントはこの監査で一切実行していない。
- 結果は §5 の表へ貼り戻す。貼り戻すまで本番状態は「未確認（結果待ち）」のまま。
- `docs/releases/` はこのブランチに無く、303f33e の release record は `claude/release-record-cap` にある（このツリーからは参照のみ）。
