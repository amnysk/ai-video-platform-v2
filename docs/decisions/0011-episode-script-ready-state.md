# ADR-0011: Episode に `script_ready` を導入する

## Status

Accepted (2026-09-04)

## Context

Phase 2 は Dummy Worker を Script Worker へ置き換え、Codex に台本を書かせる。
このとき **workflow は台本生成で終わる**（storyboard / 画像 / 動画 / 投稿は Phase 3 以降）。

問題は「台本が出来た Episode がどの状態で駐機するか」である。

- `in_progress` のまま置くと、その Episode は**自動でも人手でも出口を持たない**。
  `docs/domain/state-transitions.md`「出口の保証」に反し、stalled 検知に永久に引っかかる
- `completed` は ADR-0006 が「骨組みworkflowの正常終了」と定義した terminal 状態であり、
  ここへ落とすと Phase 3 が続きを実行できない（terminal から出る遷移は無い）
- `ready_for_review` は「**全成果物が揃った**」の意味であり、台本1本では嘘になる

つまり Phase 2 の workflow には終端が無い。ADR-0006 が骨組みworkflowに対して
`completed` を足したのと**同じ構造の欠落**である。

## Decision

**`EpisodeStatus.SCRIPT_READY` を追加する。** 遷移は2行:

- `in_progress + SCRIPT_READY` → `script_ready`（Script Worker の正常終了）
- `script_ready + STAGE_ADMITTED` → `in_progress`（Phase 3 が次工程を開始する出口）

`script_ready` は **terminal ではない**。出口を持つ非terminal状態として、
「出口の保証」を最初から満たす形で導入する。

工程ごとに状態を増やす前例にはしない。Phase 3 以降で工程が増えても、
**駐機が必要な箇所（＝そこで workflow が終わる箇所）だけ**が状態を持つ。
工程間の進行は既存の `in_progress + STAGE_SUCCEEDED → in_progress` 自己ループで表す。

## Alternatives

**(a) 状態を足さず `in_progress` の自己ループだけで表す**（Sub-agent A / E の推奨）—
語彙が増えず、ADR・doc表・遷移表・4つのテスト・DB CHECK・新 migration のコストがゼロ。
「台本が出来たか」は `jobs` 行と `artifact_metadata` 行で答えられる（INV-7）。
**採らなかった理由**: Phase 2 の workflow が終わる以上、Episode は必ずどこかで駐機する。
`in_progress` での駐機は出口の保証を破り、stalled 検知を無意味にする。
「工程完了を状態で表すな」という指摘は正しいが、**駐機点を状態で表す必要**とは別の話である。

**(b) `completed` を流用する** — 語彙を増やさない。しかし ADR-0006 が定義した
「骨組みworkflowの終端」という意味を上書きし、かつ terminal なので Phase 3 が続けられない。却下。

**(c) `ready_for_review` を流用する** — 既存の非terminal状態で出口もある。
しかし定義が「全成果物が揃った」であり、台本1本の段階で使うと
`approved → uploaded` の経路が意味を失う。却下。

**(d) Phase 3 まで Phase 2 の workflow を「未完」のまま放置する** — 論外。

## Consequences

**良い側**
- Phase 2 の workflow が正しく終端でき、Episode が出口のある状態で駐機する
- 「台本まで出来た Episode」を状態で一覧でき、UI と運用が単純になる
- Phase 3 の入口（`STAGE_ADMITTED`）が明示され、次工程の実装者が接続点を探さずに済む

**悪い側 / 引き受けた負債**
- **工程ごとに状態を足す誘惑の前例になる。** 本ADRは「駐機点だけ」と限定したが、
  それを強制する機械は無い。Phase 3 で `storyboard_ready` を足したくなったとき、
  本当に駐機するのかを問い直すこと
- 「台本が出来たか」の答えが `episodes.status` と `artifact_metadata` の2箇所から
  読めるようになった。**権威は artifact 側**（状態は駐機点の表現にすぎない）と
  決めたが、これを検査する機械は無い
- `script_ready` の出口 `STAGE_ADMITTED` は Phase 3 まで**呼び出し元が存在しない**。
  遷移表には載るがコードからは使われない期間が生じる

## 陳腐化条件

- Phase 3 で次工程が実装され、workflow が台本の先へ進むようになったとき →
  `script_ready` が本当に駐機点として必要かを再評価する
  （workflow が続けて走るなら自己ループで足りる可能性がある）
