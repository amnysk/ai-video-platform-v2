# ADR-0012: 非決定的な生成器のために Artifact の同一性を input_hash へ移す

## Status

Accepted (2026-09-04)。ADR-0010 の「Phase 2 発効」条件を発効させる。

## Context

Phase 1 の Artifact 同一性は **content-addressed** である:
キーは `artifacts/{episode_id}/{artifact_type}/{sha256}.json`、
DB 制約は `UNIQUE(episode_id, artifact_type, sha256)`。
これは「**同じ内容なら同じキー**」を表す。

`docs/failure-policy.md` §4 は再開をこう定義している:
「出力Artifactが既に存在し、同じ入力hashから作られているならskipして返す」。
しかし Phase 1 には `input_hash` を保存する列が無く、
dummy 生成器が決定論的（同じ入力→同じ内容→同じ sha256）だったため、
content-addressed キーが偶然この役を果たしていた。

**Codex（LLM）は同じ入力でも毎回違う出力を返す。** したがって:

1. 「同じ入力から作られたか」を sha256 では**判定できない**。
   再開判定が成立せず、Activity が再実行されるたびに Codex を呼び直す
   ＝ **二重生成・二重課金**
2. 2ラウンド目が成功すると `artifact_metadata` に2行目が生まれ、
   「どちらが現行の台本か」を DB が答えられない

Phase 2 の最重要要件は「外部AIが失敗・再実行されても二重生成や状態破損を起こさない」
ことなので、この欠落は Phase 2 の目的そのものを妨げる。

## Decision

**ADR-0010 が Phase 2 発効とした `version` / `input_hash` / `superseded` を、今回発効させる。**
`artifact_metadata` に3列を追加する:

- `input_hash`（NOT NULL）— この成果物を作った**入力**の指紋
- `version`（NOT NULL, 既定1）— 同じ `(episode_id, artifact_type)` 内で単調増加
- `superseded_at`（NULL 可）— NULL なら現行世代

制約:

- 既存 `UNIQUE(episode_id, artifact_type, sha256)` は**残す**（INV-11 immutable の担保）
- 追加 `UNIQUE(episode_id, artifact_type, version)`
- 追加 **partial unique index**: `superseded_at IS NULL` の行について
  `(episode_id, artifact_type)` が一意 ← 「現行の台本は常に1本」を DB が保証する

**再利用判定は2段**にする:

- **Codex を呼ばない条件（skip）**: `superseded_at IS NULL` かつ `input_hash` 一致の
  Artifact が既にある → それを返し Job を `skipped` にする
  （これが failure-policy §4 の実装であり、INV-17 を課金なしで満たす唯一の経路）
- **呼ぶ条件**: (a) `input_hash` が変わった、(b) 前ラウンドが検証で落ちた、
  (c) 人間が明示的に再生成を要求した。**この3つ以外で有料呼び出しをしない**

`input_hash` の定義（`domain/script/identity.py` の純関数）:
含める = `episode_id` / `topic` / `artifact_type` / 目標 `schema_version` /
プロンプトテンプレートIDとバージョン / 生成器ID（provider + モデル）。
含めない = ラウンド番号 / 試行回数 / 時刻 / ホスト名 / `job_id`。

## Alternatives

**(a) Phase 3 まで延期する**（Sub-agent A の推奨）— 読み手がまだ少なく、
content-addressed キーで別内容は衝突せず共存できる。
**採らなかった理由**: 「共存できる」ことと「二重生成を防げる」ことは別。
Phase 2 の要件は後者であり、`input_hash` 無しでは Activity 再実行のたびに
Codex を呼ぶ以外の実装が書けない。ADR-0010 の陳腐化条件
（「再生成を実装したとき」）に今回まさに該当する。

**(b) `input_hash` だけ足し、`version`/`superseded_at` は足さない** — 変更が小さい。
しかし2ラウンド目が成功したときに「どちらが現行か」を DB が答えられないままで、
partial unique index による**二重保存の防止**が作れない。却下。

**(c) キーに input_hash を混ぜる**（`artifacts/{episode}/{type}/{input_hash}.json`）—
列を足さずに済む。しかし同じ入力で作り直した2つ目の内容が
**1つ目を上書きする**ことになり、INV-11（immutable）に真っ向から反する。却下。

**(d) LLM の出力を決定論化する（temperature=0, seed 固定）** — content-addressed の
前提を保てる。しかし Codex CLI に seed の指定手段が無く、
モデル側の非決定性も残る。前提を守れない仮定の上に再開判定を置くのは危険。却下。

## Consequences

**良い側**
- 「同じ入力なら呼ばない」が実装可能になり、Activity 再実行が課金を伴わなくなる
- partial unique index により、現行 Artifact が2本になる事態を**DBが**防ぐ
  （アプリのバグで破れない）
- `docs/invariants.md` INV-10 の「Phase 2 発効」部分を本文から外せる

**悪い側 / 引き受けた負債**
- `artifact_metadata` に3列増え、既存行（Phase 1 の dummy）に
  `input_hash` を後埋めする必要がある。migration で `''` ではなく
  **実際に再計算できない**ため、既存行には決定論的な代替値を入れる
  （dummy は決定論的なので content sha256 を流用する）
- `version` の採番はアプリ側の責務であり、競合時の一意性は
  `UNIQUE(episode_id, artifact_type, version)` の失敗→再試行に依存する
- `input_hash` に何を含めるかの判断が**将来ずれうる**。含める要素を変えると
  過去の Artifact と一致しなくなり、全 Episode で再生成が走る。
  `identity.py` に定義を1つだけ置き、変更時は陳腐化として扱う

## 陳腐化条件

- `input_hash` の構成要素を変えるとき → 既存 Artifact との非互換を明示し、
  再生成が走ることを受け入れるか、バージョン付き hash にするかを決める
- 世代の保持期間ポリシー（`superseded` の GC）が必要になったとき
