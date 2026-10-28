# ADR-0073: 品質レポートの契約と fail-closed の投稿可否判定（純粋な契約層のみ）

## Status

Accepted (2026-10-28) — **契約と純粋な domain だけ**。配線（`upload_gate` への組み込み）は未実装で、
後続の承認済みタスクで行う（§6）。番号は `docs/architecture/v3-phase0-audit.md` の予約帯（ADR-0073 / INV-101〜104）。

## Context

設計 v3（§2.2 / §3.3 / §5.3、テスト T04・T22・T32）は、投稿前に QualityCritic と決定論ガード
（historical / geo / metadata / license / audio / visual）の結果を **Immutable な品質レポート**として残し、
「無検査（null）を PASS 扱いしない」「LLM の高評価で決定論の BLOCK を上書きしない」ことを求める。
現行の `upload_gate`（`workers/pipeline/activities.py::_refusal`）は状態・final_video の有無・予約台帳しか見ない。

`workflows.py` / Activity / `PipelineStage` / `PipelineOptions` / `*WorkflowInput` を変えると Temporal の
履歴が変わり得る。そこで本 ADR は、**配線から独立に検査できる純粋な契約・判定**だけを先に固定する。
DDL・新しい ArtifactType・新しい Episode 状態は足さない。

## Decision

> 追記（ADR-0074 §3, 2026-10-29）: `QualityReport` に `rule_version` / `expires_at` / `model` を足し `schema_version` を 2 にした。`evaluate_for_upload` は `current_rule_version` を取り、鮮度を `domain.quality.freshness.check_freshness` で判定する。以降の本文の列挙はそれ以前の形。

### 1. 契約（唯一の定義）: `contracts/quality.py`

- `QualityVerdict`: `PASS` / `WARN` / `REVIEW` / `BLOCK`。重さの順位は `VERDICT_SEVERITY` だけが持つ。
- `QualityCheck`: `gate`（`QualityGate`）・`verdict`・`reason_code`（`QualityReason`）・`detail`・`scene_id`（任意）・
  `deterministic`。`PASS ⇔ reason_code=OK` を型で強制する。
- `QualityReport`: `schema_version`・`episode_id`・`final_video_sha256` と `input_hash`（結び付き）・`generated_at`・
  `checks`・`verdict`（`aggregate` の結果）・`generator_version`・`reviewer_version`。
- 理由コード `QualityReason` は検査理由・取り込み失敗・投稿可否の結果を**1つの enum**に持つ。
- `parse_quality_report` は不備を必ず `QualityReportError(reason_code)` にする（`None` を返さない）。
  `QualityReportFailure` は「レポートを作れなかった」記録で、無検査（`None`）と区別するが扱いは同じ（拒否）。

### 2. 集計: `domain/quality/aggregate.py`（INV-102）

- 決定論の BLOCK があれば BLOCK。REVIEW があれば REVIEW。WARN は止めないが報告に残る。
- `deterministic=False`（LLM）の検査は**厳しくする方向にだけ**働く。LLM の BLOCK は REVIEW 止まり
  （LLM 単独で工程を落とさず、人の確認に回す）。LLM の PASS は何も緩めない（T32）。
- 決定論の検査が無ければ REVIEW（LLM の自己採点だけでは通らない）。検査が空も REVIEW。

### 3. 投稿可否: `domain/quality/upload_decision.py`（INV-101、T22）

`evaluate_for_upload(report, current_final_video_sha256, current_input_hash, now) -> UploadDecision(allowed, reason_code)`。
次はすべて **allowed=False**（fail-closed。`None` は PASS ではない）:
レポート無し / 作成失敗 / 取り込めない / schema_version 違い / 未知の verdict / 検査が空 /
保存された verdict が検査からの再計算と違う / final_video の sha256 が今の final_video と違う /
input_hash が違う / `generated_at` が `now` より未来 / verdict が BLOCK・REVIEW。
許可は検証済みの PASS と WARN（WARN は `UPLOAD_ALLOWED_WITH_WARNINGS` で報告を残す）だけ。

**総関数（独立レビューでの是正）**: `evaluate_for_upload` はどんな入力でも例外を投げない。想定外の例外は
`REPORT_EVALUATION_ERROR` で拒否する。追加の拒否: naive な `generated_at` / `now`（`REPORT_TIMESTAMP_INVALID`）、
深くネストした JSON（`RecursionError` を `REPORT_UNPARSABLE` に）、`checks` が `QualityCheck` の tuple でない・
直接構築された report の型不備（取り込みと同じ `validate_quality_report` を通す）、`schema_version` が厳密な
`int` でない（`1.0` / `True` は拒否）。`generated_at` が未来でも `CLOCK_SKEW_TOLERANCE`（5分。`contracts/quality.py`
に1箇所）までは許す。
**例外文の規則（INV-20/39）**: 取り込めなかった値は例外文に写さない（型名と長さだけ。`describe()`）。

**理由コードの分割について（見送り）**: `QualityReason` は検査理由・取り込み失敗・投稿可否の結果を1つの enum に
持つ（定義の単一性を優先）。検査の `reason_code` に `REPORT_*` / `UPLOAD_*` を入れられる弱点は残る。
分割は配線時（保存形式が決まるとき）に、既存の値を変えない形で行う。

**シグネチャの注記**: 依頼の `(report, sha, now)` に `current_input_hash`（キーワード必須）を足した。
input_hash の結び付きを呼び出し側が省略して fail-open になるのを避けるため、省略可能にしない。

### 4. verdict と既存 Episode 状態の対応（状態は足さない）

`domain/quality/outcome.py::episode_status_for(verdict)`:

| verdict | Episode への効果 | 根拠 |
|---|---|---|
| PASS / WARN | 状態を変えない（`render_ready` のまま投稿へ） | WARN は報告のみ |
| REVIEW | `blocked`（人の判断待ち。`blocked_reason` を持つ） | episode.md: blocked = 人の判断待ち |
| BLOCK | `needs_work`（品質ゲート未達。対象シーンだけ再生成） | state-transitions.md: 品質ゲート未達 |

**注意（後続で扱う）**: 現行の遷移表に `render_ready → needs_work/blocked` は無い。配線時は遷移表の変更
（AGENTS.md §6）を先に ADR で承認する。本 ADR は語彙の対応だけを固定し、遷移は足さない。

### 5. Agent の tool allowlist: `contracts/agent_tool_policy.py`（INV-103、T04）

許可は read-only の6つ（`research.search` / `research.fetch` / `geo.resolve` / `geo.verify` /
`bgm.list_approved` / `artifact.read`）。禁止（`fal.generate` / `youtube.upload` / `db.migrate` /
`schedule.pause` / `provider_reservations.release`）は同じ場所に1つだけ持ち、allowlist と交わらないことを
テストで固定する。`domain/quality/tool_policy.py::validate_tool_request` は許可外をすべて拒否し、
構造化された拒否（`ToolDecision`）を返す。Web 資料に埋め込まれた「upload しろ」は許可外として拒否され、
その決定が記録対象になる。本契約は tool id の語彙と許可方針だけで、実行器・登録は持たない。

**拒否の記録（独立レビューでの是正）**: 不明な tool id は記録に写さず `unknown:len=N:sha256=…` の形にする
（資料由来の秘密・注入文を残さない）。許可・禁止の既知 id だけをそのまま記録する。禁止 id の大文字小文字・
空白・NFKC 違い（`YouTube.Upload` 等）は拒否は同じだが `FORBIDDEN_ATTEMPT` として区別し、正規の禁止 id を記録する
（許可 id の似た名前は `NOT_ALLOWLISTED` のまま。許可は完全一致だけ）。

**未マージの `claude/agent-tools` 枝との関係（独立レビューでの是正）**: 同枝は `contracts/agent_tools.py` を
INV-62「tool 定義の単一宣言」（snake_case の `ToolId` / `ToolEffect` / `ToolStatus` / `ToolDef`）として持つ。
当初この契約を同じパスに置いたため add/add 衝突と2つ目の語彙になる状態だった。是正として:
(a) ファイルを `contracts/agent_tool_policy.py` に移した（`git ls-tree -r claude/agent-tools` に同名は無い）、
(b) 記号を `PolicyToolId` / `ALLOWED_POLICY_TOOLS` / `FORBIDDEN_POLICY_TOOLS` / `PolicyRejection` に改め、
agent-tools の `ToolId` と取り違えないようにした。
**`PolicyToolId`（ドット区切り）と agent-tools の `ToolId`（snake_case）は現時点で別の語彙であり、
最終的な統一は統合担当の判断事項**（AGENTS.md §8: 同じ集合を2箇所に持たない）。統一しても**禁止集合
（`FORBIDDEN_POLICY_TOOLS`）は必ず残す**こと: 許可リストに入れてはならない tool の下限であり、INV-103 の核である。

### 6. 後続（未実施）: 配線

`upload_gate._refusal()` を拡張する（**新しい stage は作らない**。設計 v3 §5.3 の「existing upload_gate」）:
final_video の sha256 を読み、品質レポートを読み出して `evaluate_for_upload` を呼び、拒否理由を既存の
`UploadGateResult.reason` に載せる。保存先（Artifact か別の表か）・DDL・遷移表の変更は別の ADR で承認してから行う。
それまで INV-104 は未検査である（→ ADR-0076 で既定 OFF の flag 付きで配線した）。

## Alternatives

- **レポートを upload_gate の中に直接書く**: 配線が先になり Activity の変更が必要。契約と判定が検査できない。却下。
- **LLM の BLOCK を BLOCK のまま通す**: 誤検知が自動で工程を落とす。REVIEW に抑え、人が見る。
- **`None` を「検査対象外」として許す**: 無検査を PASS にする穴（T22）。却下。
- **検査ごとのスコアを合成する**: 合成点が高いと BLOCK を隠せる。重さの順位だけで集計する。

## Consequences

- 契約・判定が配線前に機械検査できる。配線は後続で `_refusal()` に差す小さな変更になる。
- INV-104 は未検査のまま（正直に残す）。それまで本番の投稿可否は変わらない。
- `contracts` と `domain` の追加のみ。Temporal の履歴・DB・状態機械は変わらない。
- 理由は `docs/testing/quality-gate-rationale.md`。
