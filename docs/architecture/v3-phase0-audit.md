# v3 マルチエージェント化 Phase 0 監査・実装計画

- 作成日: 2026-10-28
- 状態: **計画（コード変更なし）**。入力: [AI_VIDEO_MULTI_AGENT_DESIGN_v3.md](./AI_VIDEO_MULTI_AGENT_DESIGN_v3.md)
- 監査方法: `claude/daily-hardening`（80775c0）のファイル読み取りのみ。本番・DB・Docker・Temporal・ネットワークには触れていない。
- 注意: 以下の file:line は監査エージェントの報告に基づく。実装担当と親の最終検証で原典を再確認すること。

## 1. 基準と番号予約

- 基準: `claude/daily-hardening`（80775c0）。ADR 0040 / INV-40 / migration 0015 が末尾。
- 未マージ枝が同じ番号帯を別内容で使用中: `agent-tools`（ADR-0047, INV-64, migration 0020）、`budget-zero`（ADR-0061, INV-84, migration 0018）、`jpmas2-*`（ADR-0041〜0046）。
- **この作業の予約帯**（上記と衝突しない）:

| 枝 | ADR | INV | migration |
|---|---|---|---|
| v3-avpu-description | 0070 | INV-90〜92 | 使わない |
| v3-geo-contracts | 0071 | INV-93〜96 | 使わない |
| v3-bgm-contracts | 0072 | INV-97〜100 | 使わない（カタログDDLは後続枝で採番） |
| v3-quality-gate | 0073 | INV-101〜104 | 使わない |

- 欠番は許容する（AGENTS.md §6 は再利用を禁止するだけ）。統合時に親が衝突を解消する。
- 本番の稼働SHA（1227e26 系か daily-hardening 系か）は未確認。**確認前は、DB・Schedule・deploy に関わる有効化を一切しない。**

## 2. v3 と現行ADRの食い違い（設計判断が必要）

1. Remotion 前提 → ADR-0019 が却下済み。mix・地図合成は ffmpeg 前提とする。
2. AgentRunner 新設 → `agent-tools` に Agent Runtime（ADR-0043/0047）が既にある。**本作業は AgentRunner を新設せず、Phase 2 では薄い契約に留め統合を別ADRで決める。**
3. 検証ゲートが助言止まり（ADR-0038 B6 §4）→ geo/QualityGate は fail-closed の別工程にする。
4. 「最大2往復」→ ADR-0038 が自動書き直しループを移植しない決定と衝突。有料呼び出しは台帳経由とADRに明記。
5. P3 canary → `budget-zero`（fal保留）との整合を canary 計画書で確認。

## 3. 再利用マップ

| v3 概念 | 既存 |
|---|---|
| EvidenceBundle / ClaimLedger | `contracts/research_evidence.py`（EvidenceClaim/Source/Link）、ADR-0038 |
| Research Gateway | ADR-0036/0037、`infrastructure/research/gateway.py` |
| 決定論ガード優先 | ADR-0038 の `min(提案, コード評価)` |
| 成功sceneの再利用 | ADR-0018/0033、INV-31/33 |
| 課金・再送禁止 | ADR-0013、INV-15 |
| 投稿前ゲート | `workers/pipeline/activities.py` `upload_gate` / `_refusal()` |
| 二重投稿照合 | `contracts/upload.py`、`workers/upload/activities.py`、ADR-0020 |
| 技術QA | `domain/render/qa.py` |
| ログ契約 | ADR-0040、`contracts/log_contract.py`、INV-38〜40 |

## 4. 実装計画（波1: 互いに独立。いずれも有効化しない）

| 枝 | 内容 | 主なテスト |
|---|---|---|
| v3-avpu-description | description から `avpu` を除去（flag・既定OFF）、metadata lint、5000B超の保留。tags の marker と旧形式照合は不変 | T17, T19, T29 + marker回帰 |
| v3-geo-contracts | `GeoPlace/GeoManifest/GeoVerdict` と純関数の検証器、fakeフィクスチャ | T07, T08, T09, T11, T32 |
| v3-bgm-contracts | `MusicCueSheet`、トラック/ライセンス契約、承認済みのみ選曲の純関数 | T12, T13, T14 |
| v3-quality-gate | `QualityReport` 契約と fail-closed 判定の純関数（`upload_gate` への結線は後続） | T22, T32 |

波2（後続・個別承認）: `upload_gate` 結線、`_reserve` round2 の照合エラー区別、BGMカタログDDLとRender mix、決定論的map render、ログ語彙追加、AgentRunner統合方針。

## 5. Risk Register

| # | リスク | 緩和 | 状態 |
|---|---|---|---|
| R1 | replay差（入力dataclassの欄追加、PipelineStage追加） | 欄・stageを足さない。flagはworker env。旧履歴replayを必須 | 設計で回避 |
| R2 | description から marker を外すと照合が弱まる | tags維持を別テスト化。tags単独の実機照合が確認できるまで切替禁止 | **本番切替ブロック** |
| R3 | `_reserve` round2 が照合エラーと不在を区別しない | 波2で修正（エラーはUnknown） | 未対応 |
| R4 | `upload_gate` が無検査をPASS扱い | 波2で `quality_report` 必須化 | 未対応 |
| R5 | BGM権利（Shorts不可・帰属・失効） | 承認済みのみ選曲、帰属全文を説明欄へ、収まらなければBLOCK | 契約で担保 |
| R6 | 史実・地図の誤り | 決定論ガード優先、地図はGeoManifestから描画 | 契約で担保 |
| R7 | API費用・budget-zero | agentから課金経路を遮断（architecture test）、台帳経由のみ | 波2 |
| R8 | 番号衝突（ADR/INV/migration） | 本書の予約帯。統合時に親が確認 | 管理中 |
| R9 | 本番SHAが daily-hardening 系でない可能性 | オーナーのread-only確認待ち | **未確認** |
| R10 | 現行descriptionがナレーション全文（設計§5.1違反） | 波2で紹介文+出典+クレジットへ | 未対応 |
| R11 | 設計書のRemotion前提 | ffmpeg前提に改訂（別途承認） | 未対応 |

## 6. オーナーが実行するread-only確認（私は実行しない）

`git rev-parse HEAD`（本番ホスト）、`./scripts/workers-versions.sh`、`alembic current`、`python scripts/schedule-guard.py status --json`、`temporal workflow list --query 'ExecutionStatus="Running"'`、`provider_reservations` の未照合行のSELECT、`git branch -a --contains 1227e26`、`docker stats --no-stream`。結果を共有いただければ R9 を解消する。
