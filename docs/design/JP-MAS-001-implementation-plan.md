# JP-MAS-001 v2.0 実装計画（作業分担・順序・境界）

この文書は**作業の進め方**であり、システムが満たす条件ではない。条件は
[設計 JP-MAS-001-v2.0](./JP-MAS-001-v2.0.md)、repo への対応付けは
[docs/domain/intervention.md](../domain/intervention.md)、決定は
[ADR-0041](../decisions/0041-decision-records-and-intervention.md)。

## 0. 前提（2026-10-07 実査）
- 統合 branch `claude/jpmas-integration`（`claude/daily-hardening` 80775c0 から）。各担当は
  `.worktrees/claude-jpmas-<x>` / branch `claude/jpmas-<x>` を統合 branch の土台 commit から切る。
- 本番 compose project `avp2` に触れない。integration は `deploy/logging/test/run-e2e.sh`
  （project `avp2-oslog-c*`、internal network、ポート非公開）か、`TEST_DATABASE_URL`（`_test`）だけ。
- LLM・fal・YouTube は fake。`tests/live/` は使わない。

## 状態（2026-10-15）

実装の存在を `infrastructure/` `apps/` `workers/` `scripts/` で確認した範囲（動作の保証ではない）:

- 実装あり（unit / contract / architecture の試験あり）: A（`capture` / `records` / `manifests` / `read_model` /
  `review_packet` / `reviewer`、migration 0016）、B（`commands` / `changes` / `impact` / `budget` / `gate`、
  `workers/intervention`、`resolve_annotation` = migration 0017）、C（`apps/api/routers/intervention*.py`、
  `scripts/intervene.py`）、D のうち投影・再投影・`rebind`・`consistency-check`・backup drill のスクリプト、
  G（対話 AI アシスタントの adapter `apps/assistant`、ADR-0042）。
- 追加: 判断記録・実行記録と Episode・Scene・Workflow の結び付き（migration 0018、`temporal_context`）。
- **未完了**: `tests/integration/test_intervention_*`（隔離環境の一連試験・故障試験）はこの branch に無く、
  `claude/jpmas-verify` で追加中。**完了とは扱わない**。隔離環境での backup/restore 検証と、compose への
  `TOPIC_PLANNER_CAPTURE_ENABLED` 受け渡しも未実施。

## 1. 土台（親、統合 branch に直接）
`contracts/intervention.py`（語彙・payload）、`contracts/roles.py`（RoleSpec）、migration 0016 と
`infrastructure/db/models.py` の9表、`infrastructure/intervention/store.py`（全担当が使う唯一の永続化の入口）、
`infrastructure/intervention/manifests.py`（`current_members` / `ensure_manifest`）、
`domain/errors.py` の `EpisodeHeldError` / `MissingDecisionRecordError`（どちらも `NeedsInputError`）、
ADR-0041、`docs/domain/intervention.md`。**契約（上記ファイル）を変えたい担当は親に依頼する**（各自で書き換えない）。

## 2. 担当と書き込み範囲

| 担当 | 書いてよいファイル | 主な成果 |
|---|---|---|
| A 記録・読み取り | `infrastructure/intervention/{capture,records,manifests,read_model,review_packet,reviewer}.py`、`contracts/review_packet.py`、`workers/planning/*`・`workers/storyboard/activities.py` の記録の呼び出し追加、`docs/domain/review-packet.md`、対応する tests | AgentRun/ContextSnapshot の捕捉、Producer/Writer-Director の DecisionRecord、manifest・revision link の導出、read model（Episode・判断・比較・stale）、ReviewPacket JSON/Markdown、変更案ファイルの形、独立レビュアー |
| B 介入の適用 | `infrastructure/intervention/{commands,changes,impact,budget,gate}.py`、`workers/intervention/*`（新 worker）、`workers/pipeline/{workflows,activities}.py` の control gate、`contracts/pipeline.py` の `EpisodePipelineInput.control_gate` と `PipelineOutcome.PARKED`、`infrastructure/db/repositories.py` の `mark_dispatched` / `mark_upload_dispatched` への gate、`infrastructure/temporal/schedules.py` の再配送 Schedule、対応する tests | command の受付（API から呼ばれる関数）・適用・冪等、ChangeSet 提案・影響・見積り・適用、競合、hold/HELD、resume、checkpoint、outbox 再配送、Temporal 接続、replay 互換 |
| C API・CLI | `apps/api/routers/intervention.py`、`apps/api/auth.py`（principal）、`apps/api/main.py` の router 登録、`infrastructure/config.py` の介入設定、`scripts/intervene.py`、`docs/operations/intervention-api.md`、対応する tests | read / command API（権限・scope 記録・409/422/503）、コメント、差分・影響費用、ReviewPacket 取得、変更案取り込み、CLI |
| D 既存接続・ログ・試験 | `contracts/log_contract.py`・`docs/observability/*`・mapping 再生成、`infrastructure/intervention/{projection,rebind}.py`、production / voice / render / upload の「storyboard は現行台本から」の検査を `rebind` 経由にする変更、`EpisodeHeldError` の失敗分類、`scripts/intervention-reproject.py`・`scripts/consistency-check.py`、`tests/integration/test_intervention_*`、`docs/operations/intervention-runbook.md`、`docs/testing/intervention-rationale.md` | 新イベントの語彙と発行（commit 後）、再投影、rebound の単一関数、停止の失敗分類、隔離環境の一連試験・故障試験・回帰、backup/restore 検証 |

相互レビュー: A↔B（記録と適用の transaction 境界）、C↔D（API と試験）。最後に独立した確認（親が新しい reviewer を起動）。

## 3. 担当間のインターフェース（実装前に固定）

- **土台（親）**: `manifests.ensure_manifest(session, store, episode_id, actor) -> Manifest`（現行から採用 manifest を作る／既存を返す）、
  `manifests.current_members(session, episode_id) -> dict[slot, revision_id]`。
- **store**: `InterventionStore`（`infrastructure/intervention/store.py`）。Episode 内を書き換える処理は
  `lock_control` → 書き込み → `append_event` を**同じ session・同じ commit**で行う。
- **A が提供**:
  - `capture.AgentRunRecorder`（`record(...) -> AgentRun`、snapshot を MinIO `agent-runs/{scope}/{agent_run_id}/` に書いてから行を作る）
  - `read_model.EpisodeReadModel.load(episode_id) -> EpisodeView`、`compare_revisions(a, b)`、`approval_is_current(annotation, control)`
  - `review_packet.build_packet(view) -> ReviewPacket`、`render_markdown(packet) -> str`、`ChangeProposalFile`（取り込み形式）
- **B が提供**:
  - `commands.receive_command(session, episode_id, envelope, principal) -> ReceiveResult`（API が1 transaction で呼ぶ。hold は受付で HOLD_REQUESTED）
  - `commands.apply_command(session_factory, artifact_store, command_id) -> CommandOutcome`（Activity の本体。冪等）
  - `gate.check_dispatch_allowed(session, episode_id)`（`mark_dispatched` から呼ぶ。`EpisodeHeldError` / `MissingDecisionRecordError`）
  - `impact.estimate(change_set, ...) -> Impact`、`budget.remaining(...) -> BudgetView`
- **C が提供**: HTTP の形（`docs/operations/intervention-api.md`）。API は store・commands・read_model を呼ぶだけ（INV-16）。
- **D が提供**: `rebind.bound_script_ref(session, storyboard_meta, storyboard) -> ScriptRef`（構成の同じ新台本への
  結び直しを考慮した「この storyboard が使う台本」。production / voice / render が唯一ここを通る）、
  `projection.emit_event(event)`（commit 後の発行）と再投影。

## 4. 順序
1. 親: 土台を commit（この文書と同時）。
2. A・B・C・D を並行。B の `receive_command` の型と A の `ensure_manifest` を最初に push し合う。
3. 親: A→B→C→D の順で統合 branch へ merge（merge commit。rebase しない）。衝突は親が解消。
4. D: 統合後の branch で隔離環境の一連試験（設計 §17 の13項目）。
5. 相互レビュー → 独立確認 → 修正 → 最終検証（ruff / pyright / unit / contract / architecture / 隔離 integration）。

## 5. 完了の定義（各担当）
- TDD（失敗するテストを先に）。既存テストは変えない（変えるなら理由をコミットに）。
- 新しい不変条件（INV-41〜47）は、強制するテストと同じコミットで `docs/invariants.md` に足す。
- テストを足した理由を `docs/testing/intervention-rationale.md` に書く（親が統合時にまとめる）。
- `ruff check . && ruff format --check . && pyright && pytest tests/unit tests/contract tests/architecture` が通る。
