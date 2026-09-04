# ADR-0014: LLM出力の形式不正は `retryable` であって `permanent` ではない

## Status

Accepted (2026-09-04)。`docs/failure-policy.md` を改訂する。

## Context

`docs/failure-policy.md` §1 は `permanent` の説明で
「入力自体が不正で再実行しても同じ（**スキーマ違反**、存在しない素材）」と例示している。
また `permanent` の条件を「**同じ入力で必ず同じ失敗になる**と示せる場合だけ」と定めている。

この2つは、決定論的な入力に対しては整合する。
しかし **LLM は同じ入力でも違う出力を返す**ため、Codex 出力の形式不正に対しては
両者が矛盾する:

- 出力が invalid JSON（截断、前置きの混入、コードフェンス）
- パースはできたが Pydantic の `ValidationError`（必須欠落、シーン数超過）

これらは**次のラウンドで直る典型**であり、「同じ入力で必ず同じ失敗になる」ことを
示せない。`permanent` に分類すると Episode が即 `failed`（terminal）になり、
1回の生成揺れで作品が失われる。INV-12 の趣旨にも反する。

## Decision

**LLM 出力の形式不正は `retryable` に分類する。**

- `ScriptOutputUnparseableError(RetryableError)` — JSON としてパースできない
- `ScriptSchemaViolationError(RetryableError)` — パースできたがスキーマ違反

ただし**同じ `input_hash` で規定ラウンド連続して同種の違反**が続く場合は、
生成揺れではなく**プロンプトとスキーマの不整合**（入力側の欠陥）なので
`PromptContractError(NeedsInputError)` へ昇格させ、Episode を `blocked` にして
人間に回す。`permanent` にはしない — プロンプトを直せば回復するため。

`permanent` に残るのは「入力 Artifact が存在しない / 未知の schema_version」だけ。
Script Worker には実質 `permanent` がほぼ存在しない。これは
failure-policy §2 の terminal 3条件を素直に適用した帰結であり、意図的である。

**`docs/failure-policy.md` §1 の `permanent` の例から「スキーマ違反」を外し、
「**決定論的な**入力の不正」と限定する。**

## Alternatives

**(a) 現状維持（スキーマ違反＝permanent）** — 文書を変えずに済む。
しかし1回の生成揺れで Episode が terminal `failed` になる。
「retry可能なJob失敗だけでEpisodeをterminal failedにしない」（INV-12）の趣旨に反する。却下。

**(b) すべて `transient` にして Temporal の自動retryに委ねる** — 実装が最も単純。
しかし課金呼び出しが予約台帳（ADR-0013）を通らずに増える。却下。

**(c) 出力を修復する（截断JSONの閉じ括弧を補完する等）** — 成功率は上がる。
しかし `docs/domain/artifact.md` の「想定外は推測して読まない」に反し、
壊れた台本を正常として通す経路を作る。却下。**修復はしない。**

**(d) 昇格ルールを設けない（永久に retryable）** — 単純。
しかしプロンプトとスキーマが恒久的にずれている場合、
枠を使い切るまで無駄な課金呼び出しを繰り返す。却下。

## Consequences

**良い側**
- 生成揺れで作品を失わない
- 恒久的な不整合は `blocked` として人間に届き、自動修復に流れない（INV-12）
- 失敗クラスが例外型から導出される原則を維持（散文の grep をしない）

**悪い側 / 引き受けた負債**
- **`permanent` がほぼ空になった。** terminal `failed` へ至る経路が細く、
  本当に永久に直らない失敗が `blocked` に溜まる可能性がある
- 昇格ルール（N ラウンド連続で `needs_input`）の N は運用値であり、
  最適値の根拠は無い。Phase 2 では `jobs.max_attempts` を流用する
- 「同じ入力で必ず同じ失敗になる」の判定は人間の設計判断であり、
  機械検査できない。分類を間違えると静かに retry 予算を食う
