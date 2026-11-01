# ADR-0085: AI 品質レビューは Agent Runtime の LLM port と ModelPolicy を再利用し、決定論の判定を最後の安全装置にする（設計のみ・既定 OFF）

## Status

Proposed（v3 Phase 3 の親エージェント判断。2026-10-30 JST。**設計のみ**。実装・有効化は
Agent Runtime（`claude/agent-tools`、ADR-0043 / ADR-0047）の統合と、所有者の承認を前提にする）

## Context

実査（2026-10-30、read-only）:

1. 本番（e3c9555）にある AI 呼び出しは **Codex CLI だけ**（`infrastructure/providers/codex_cli.py`、
   `codex_scene_alternative.py`）。model の選択は `contracts/codex_models.py` の allowlist（`CODEX_ALLOWED_MODELS`）と
   事前診断（ADR-0058）。予約台帳には `codex_script` / `codex_storyboard` が `estimated_cost_usd = NULL`（未計測）で載る。
2. 未マージの Agent Runtime（`claude/agent-tools` 2fb3b41、土台 80775c0 = v3 以前）には、すでに次がある:
   - `domain/agents/ports.py::AgentLlm`（`generate_json(prompt, schema, max_tokens) -> LlmResult{data, model, tokens}`）
   - `contracts/agents.py::ModelPolicy` と `MODEL_POLICIES`（`policy_id@version`、provider、model、単価。単価の無い
     policy は費用を測れず `None`）と `resolve_model_policy`
   - `workers/agents/llm.py::RecordingLlm` / `ReplayLlm`（応答を台帳の evidence に残し、再試行で LLM を呼び直さない）
   - `domain/agents/handlers/quality.py`（`quality_reviewer_v1`: 入力欠落なら LLM を呼ばない、所見を決定論で検証、
     修正は**提案**のみ）
   - `infrastructure/agents/codex_llm.py`（Codex CLI を `AgentLlm` として包む）
   この基盤は v3 Phase 2 の土台（7ae20e8）と 425 ファイル差分で乖離しており、Phase 3 の範囲では統合しない。
3. Phase 2 の品質契約（`contracts/quality.py`）は LLM の結果を受ける形をすでに持つ:
   `QualityCheck.deterministic=False`（厳しくする方向にだけ働く）、`QualityGate.CRITIC`、`QualityModelIdentity`
   （provider + model id）、鮮度の `rule_version` / `expires_at`（ADR-0074）。`QualityCheck.detail` は固定の
   ASCII 文だけ（INV-106）。
4. ADR-0076 §5 は LLM レビューを実装せず、Anthropic API の採用は新しい外部サービス = 技術選定 ADR・費用上限・
   台帳予約（INV-15）が先、とした。

## Decision

**AI レビューは「決定論の品質レポートに `deterministic=False` の CRITIC 検査を足すだけの、交換可能な
CheckProvider」として作る。LLM の呼び出しは Agent Runtime の `AgentLlm` port と `ModelPolicy` を通し、
二つ目の runner・model registry・台帳経路を作らない。最終の投稿可否は常に決定論の判定が決める。**

### 1. 位置づけ（安全側にしか動かない）

- AI レビューの出力は `QualityCheck(gate=CRITIC, deterministic=False)` の列。集計規則（ADR-0073 §2）により、
  AI の PASS は決定論の REVIEW / BLOCK を緩めない。AI が REVIEW / BLOCK を出せば判定は厳しくなる。
- AI の所見は**固定の理由コード**（`QualityReason`）と固定文の `detail` に写してから保存する。モデルの自由文は
  `QualityCheck` に入れない（INV-106）。自由文は監査用 Artifact に置き、gate は読まない。
- AI レビューが失敗・時間切れ・予算拒否のとき: AI 検査を「必須」にしている構成では REVIEW（fail-closed）、
  「任意」の構成では検査を欠いたまま決定論の判定に従う。既定は**任意**（決定論の検査が揃えば投稿可否は変わらない）。

### 2. model の交換とエスカレーション

- model は `ModelPolicy` の参照（`policy_id@version`）で選ぶ。品質レビュー用に 2 つの役を置く:
  - `quality_review_standard` — 通常のレビュー（Claude Sonnet 相当を第一候補）
  - `quality_review_escalation` — 複雑な判断（Claude Opus 相当を第一候補）
  役から policy への対応は設定の 1 箇所（AGENTS.md §8）。policy を差し替えても呼び出し側は変わらない。
- エスカレーションの条件は**決定論**で決める（モデルに自分の昇格を決めさせない）: 通常レビューが REVIEW / BLOCK を
  返した、決定論の検査に `HIGH_IMPORTANCE_UNVERIFIABLE` / `HISTORICAL_SOURCE_CONFLICT` / `GEO_*` がある、
  通常レビューの応答が schema 違反だった（1 回まで）。1 Episode あたり最大 1 回。
- レポートの `model` には実際に応答した policy の provider / model id を入れる（`QualityModelIdentity`）。
  policy や model が変われば `rule_version` の比較で古いレポートは使えない（ADR-0074 / INV-107）。

### 3. 費用と承認

- 呼び出しは必ず予約台帳を通す（INV-15）。`RecordingLlm` の evidence を予約に残し、再試行・replay では
  `ReplayLlm` で呼び直さない（二重課金の防止）。
- 新しい有料 provider（Anthropic API など）を使う policy は、(a) 技術選定 ADR、(b) 月間上限（fal の
  ADR-0063 と同じく予約と同じトランザクションで強制）、(c) **所有者本人の承認記録**（ADR-0080 の
  `ApproverKind.OWNER`。`agent_delegated` では足りない）が揃うまで選択できない。既定の設定は `fake` か無効。
- Codex CLI（既存の契約内）を第一の実装にしてよいが、これも flag（既定 OFF）の背後に置き、日次で ON にする判断は
  所有者が行う。

### 4. 実装の順序（本 ADR では書かない）

1. Agent Runtime（ADR-0043 / ADR-0047）を v3 の土台へ統合する（別の承認・別ブランチ）。
2. `domain/agents/handlers/quality.py` の所見を `QualityCheck(CRITIC)` に写す変換を純関数で作る（決定論で検証）。
3. Phase 3 の品質記録（ADR-0081）の CheckProvider 列に、flag の背後で足す。
4. fake LLM での単体・契約テスト、replay テスト、隔離 integration。実 API は呼ばない（AGENTS.md §9）。

## Alternatives

- **Phase 3 で Anthropic SDK を直接呼ぶ小さなクライアントを作る**: すぐ動くが、Agent Runtime の port・policy・
  台帳経路と二重になり、統合時にどちらかを捨てることになる。費用上限と承認の仕組みも二重に要る。採らない。
- **Agent Runtime を Phase 3 で丸ごと統合する**: 425 ファイル・約 8 万行の差分で、土台が v3 以前。今回の
  QualityGate / BGM / 地図と同時に入れるとレビューが表層的になる（AGENTS.md §3）。別の作業として扱う。
- **LLM に最終判定を任せる / LLM の PASS で REVIEW を解除する**: 決定論の安全装置を失う。ADR-0073 §2 と矛盾。採らない。
- **モデルにエスカレーションの要否を判断させる**: 費用が読めず、プロンプト注入で昇格を誘発できる。採らない。

## Consequences

- 良い: AI レビューを足しても投稿可否は厳しくなる方向にしか変わらない。model の交換は policy の差し替えで済む。
  二重実装を避けられる。
- 悪い / 引き受けた負債: Phase 3 では AI レビューは**動かない**（設計のみ）。Agent Runtime の統合が前提で、
  その統合は大きい。AI 検査を「必須」にする構成は、AI 呼び出しの可用性に投稿を依存させる（既定は任意）。
- 有料 model の本番有効化は、本 ADR の Accepted だけでは許されない（§3 の所有者承認が別に要る）。
