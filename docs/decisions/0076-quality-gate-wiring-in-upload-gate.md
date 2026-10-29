# ADR-0076: 品質ゲートを `upload_gate` に結線する（既定 OFF・状態を足さない・Phase 2 は決定論のみ）

## Status

Accepted（v3 Phase 2 の親エージェント判断。2026-10-29。既定 OFF のため本番挙動は変わらない。
本番で ON にする判断は所有者だけが行う）

## Context

ADR-0073 は `QualityReport` 契約と fail-closed の `evaluate_for_upload` を定めたが、配線は後続とした
（INV-104 は「配線は未実装」）。Phase 2 監査（`docs/architecture/v3-phase2-audit.md`）で次が分かった。

1. `workers/pipeline/activities.py::_refusal()` に判定を足すだけなら、PipelineStage・Episode 状態・Activity・
   Activity の入出力 dataclass を変えずに済む。Activity の本体は replay されないので replay 互換。
2. ADR-0073 §4 の `REVIEW → blocked` / `BLOCK → needs_work` は、遷移表に `render_ready → blocked/needs_work`
   が無いため、そのままでは使えない。
3. レポートを Artifact として保存すると `artifact_metadata.artifact_type` の CHECK 拡張（migration）が要る。
4. 手動の `POST /episodes/{id}/upload` は `upload_gate` を通らない。
5. レポート不在を拒否にすると、既存の gate テスト（`tests/unit/test_pipeline_activities.py`、
   `tests/unit/test_pipeline_workflows.py`）の期待が変わる。
6. 本番ベース（303f33e）には Anthropic を呼ぶ基盤が無い。AI 呼び出しの既存基盤は Codex CLI 経路である。

## Decision

### 1. 既定 OFF の flag で結線する

設定キー `AVP_QUALITY_GATE_ENFORCE`（既定 OFF。定義は設定の1箇所）。

- **OFF**: `_refusal()` の挙動は現行とバイト単位で同じ。既存テストは変更しない（AGENTS.md §2）。
- **ON**: 既存の拒否理由（存在・状態・final_video・予約）をすべて先に評価し、通った場合だけ品質判定を行う。
  品質判定で拒否した場合は `UploadGateResult.reason` に `quality: <QualityReason の値>` を載せる。
  理由文に未検証の文字列を写さない（INV-20/39、ADR-0073 §3 の例外文の規則）。

### 2. 状態を足さない。REVIEW / BLOCK は「拒否のみ」

| 判定 | 自動経路の結果 |
|---|---|
| PASS / WARN | 他の投稿条件を満たせば許可（WARN は報告に残す） |
| REVIEW | 拒否。Episode は `render_ready` のまま、pipeline は既存の `UPLOAD_SKIPPED` で終わる |
| BLOCK | 拒否（同上） |
| レポート不在・作成失敗・評価エラー | 拒否（fail-closed。PASS に倒さない） |

止まった Episode は既存の watchdog の upload 期限検査が検知する（新しい通知経路は作らない）。
ADR-0073 §4 の `episode_status_for` は**配線しない**（語彙の対応として残す）。遷移表は変えない。

### 3. Phase 2 のレポートは gate 内で決定論的に作る（保存 DDL なし）

ON のとき `upload_gate` は、その時点の current な final_video（sha256）と既存の成果物から、決定論の検査だけで
`QualityReport` を組み立て、ただちに `evaluate_for_upload` に渡す。作った直後に同じ sha256 で評価するので、
別の成果物・古い成果物のレポートを使い回す経路が構造上ない。それでも評価は必ず `evaluate_for_upload`
（ADR-0074 の `check_freshness`: artifact hash・input hash・rule version・期限）を通す。
現行のルール版 `current_rule_version` は `contracts/quality.py` の定数1箇所に置き、gate はそこから読む。

- 検査の入力は既存の成果物だけ（技術 QA、地図の GeoVerdict、BGM のライセンス・クレジット、説明欄の lint）。
  入力が無い検査は「該当なし」ではなく ADR-0073 §2 の集計規則に従う（決定論の検査が1つも無ければ REVIEW）。
- 判定の証跡は既存のログ契約（ADR-0040）で構造化して出す（理由コード・対象 ID・sha256・ルール版）。
  レポート本体の永続保存（Artifact 化）は**後続の ADR と migration 計画**で決める。
- 新しい Activity・有料 API 呼び出しは無い。

### 4. 手動投稿経路

flag ON のとき、`POST /episodes/{id}/upload` も同じ決定論レポートを評価し、**BLOCK とレポート作成失敗は拒否**する。
REVIEW は手動経路では許可する（人が明示的に投稿を指示したことを人の承認とみなす）。承認した事実は構造化ログに残す。
DB に承認記録を持つことは後続 ADR の対象とする。flag OFF では手動経路は現行どおり。

### 5. LLM レビューと自動修正ループは Phase 2 では実装しない

- LLM による品質レビューは実装・有効化しない。モデル選択の設定キー（通常: Claude Sonnet、高難度: Claude Opus）は
  予約のみとし、呼び出し経路は作らない。Anthropic API を使う場合は新しい外部サービスの追加にあたるため、
  技術選定 ADR・費用上限・台帳経由の予約（INV-15）を先に決める。AgentRunner を新設しない。
- 最終の安全判定は常に Python の決定論ルール（ADR-0073 §2: LLM の PASS は何も緩めない）。
- 自動修正ループ（最大2回の候補）は ADR-0038 の「自動書き直しループを移植しない」決定と衝突するため、
  本 ADR では扱わない。

### 6. 実装の記録（設定キーと単一の定義元）

- 設定キー: `contracts.quality.QUALITY_GATE_ENFORCE_ENV`（`AVP_QUALITY_GATE_ENFORCE`）。現行規則版:
  `QUALITY_RULE_VERSION`（同ファイル）。`Settings.quality_gate_enforce` は前者を `validation_alias` に使う。
- `grep -rn "AVP_QUALITY_GATE_ENFORCE\|quality_gate_enforce\|QUALITY_GATE_ENFORCE_ENV" apps/ workers/ domain/
  infrastructure/ contracts/ docs/`（実装時点。ADR 本文を除く）: 書き手（定義）2 =
  `contracts/quality.py`・`infrastructure/config.py`、読み手 2 = `workers/pipeline/run_worker.py`（worker へ渡す）・
  `apps/api/routers/episodes.py`（手動経路）、無関係 0。`QUALITY_RULE_VERSION` の読み手 2 =
  `workers/pipeline/activities.py`・`apps/api/routers/episodes.py`。literal の再定義は無い。
- gate 時点で新しい保存先なしに読めるのは final_video の `sha256` / `input_hash` だけ（技術 QA の本体は
  object storage、説明欄は upload 工程で組み立てる）。よって既定の provider 列は技術 QA・説明欄の検査を
  持つが、gate はそれらの入力を渡さず、**本番既定では決定論の検査が 0 件 = REVIEW = 拒否**になる。
  入力の供給元（レポートの永続化）と地図・BGM の provider は後続で `CheckProvider` 列に足す。
- 構造化ログは既存語彙のみ: 拒否は `upload.skipped`（outcome=blocked）、許可・手動承認は `log.record`。

## Alternatives

- **Episode 状態を足す / 遷移表に `render_ready → blocked` を足す**: 人の判断待ちを状態で表せるが、
  状態機械の変更と replay 検証が必要。Phase 2 は「止まる」ことだけを保証すれば足りるので見送る。
- **レポートを新しい ArtifactType として保存**: 監査に強いが CHECK 拡張の migration が要り、本番に適用できない。
  後続で expand-only の migration 計画とともに行う。
- **レポート不在を常に拒否（flag なし）**: 既存テストの期待を変えることになり、AGENTS.md §2 の承認手続きが要る。
  本番挙動も即座に変わるため採らない。

## Consequences

- flag OFF の本番挙動は不変。ON にすると、決定論の検査が揃わない Episode は自動投稿されない（REVIEW）。
  これは意図した fail-closed であり、ON の前に各検査の入力が揃っていることを確認する必要がある。
- INV-104 を「flag ON のとき強制」に改め、強制するテストを同じコミットで置く。
- 後続 ADR: レポートの永続保存と migration、DB 上の承認記録、LLM レビューの技術選定と費用上限。
