# ADR-0006: 骨組みworkflow用にEpisode `completed` とJob状態語彙を定める

## Status

Accepted (2026-09-04)

## Context

Phase 1 の目的は「1つのEpisodeが Temporal / PostgreSQL / MinIO を使って安全に
状態遷移する最小の縦切り」を作ることであり、企画・素材生成・レンダー・投稿の
本実装は行わない。

しかし Phase 0 で定めた Episode 状態表には、骨組みworkflowの**終端がない**。
表の正常系は `in_progress → ready_for_review → approved → uploaded → analyzed` で、
どれも「人間の承認」や「YouTube投稿」を前提とする。骨組みが到達できる
terminal は `failed` と `cancelled` しかなく、成功したのに terminal へ行けない。

また Job の状態語彙も Phase 0 では `running / succeeded / failed / skipped` +
`failure_class` だったが、retry の可否が status から読めず、
「retryable な失敗で止まっている Job」と「もう打ち切った Job」を
UI・運用が区別できない。これは INV-12 が守られているかを外から確認できない、
ということでもある。

## Decision

**(a) Episode に terminal 状態 `completed` を追加する。**
遷移は `in_progress + SKELETON_COMPLETED → completed` の1行だけ。
本番パイプラインが載ったら正常系は `ARTIFACTS_READY → ready_for_review` を通り、
`completed` は骨組みworkflow専用の終端として残る。

**(b) Job の状態語彙を
`queued / running / succeeded / retryable_failed / terminal_failed / skipped` とする。**
`failure_class`（`transient` / `retryable` / `needs_input` / `permanent`）は
列として残し、status との関係を1つに固定する:

| failure_class | job status |
|---|---|
| `transient` / `retryable` | `retryable_failed`（試行枠が残る間） |
| いずれも枠切れ | `terminal_failed` |
| `needs_input` / `permanent` | `terminal_failed`（retryしない） |

**Job が `terminal_failed` でも Episode は terminal とは限らない。**
Episode の遷移先は `episode_event_for_failure()` が失敗クラスからのみ決める（INV-12）。

**(c) 属性名を `attempts` / `max_attempts` / `type` に固定する**（Phase 0 の
`attempt` / `stage` を置き換える）。

## Alternatives

**(a-1) `analyzed` を骨組みの終端として流用する** — 新しい状態を増やさずに済む。
しかし `analyzed` は「実績を回収し学習へ反映済み」という意味を持ち、
何も投稿していないEpisodeがそこに居ると、実績集計と学習の母集団が汚れる。却下。

**(a-2) 骨組みworkflowを `in_progress` で終わらせる** — 表を一切変えずに済む。
しかし `in_progress` のまま完了したEpisodeは stalled 検知に引っかかり続け、
「出口の無い状態を作らない」という保証が意味を失う。却下。

**(b-1) Phase 0 の `failed` + `failure_class` を維持する** — 変更ゼロ。
しかし status だけを見る読み手（UI一覧、Prometheus のラベル、運用）が
retry可能性を判断できず、`failure_class` を各所で再解釈することになる。
「同じ真実が2箇所に散る」という、このrepoが避けたい形そのもの。却下。

**(b-2) status を廃して failure_class だけにする** — 語彙は1つになる。
しかし成功・実行中を表せない。却下。

## Consequences

**良い側**
- 骨組みworkflowが正しく終端でき、`test_every_non_terminal_status_can_reach_a_terminal_status`
  が意味のある保証になる
- Job status を見るだけで retry 可能性が分かる。UIとメトリクスが単純になる
- INV-12 が「Job が terminal_failed でも Episode は blocked」という形で
  テスト可能になった（`test_exhausted_retryable_failure_blocks_instead_of_failing`）

**悪い側 / 引き受けた負債**
- **正常系の終端が2つになる**（`completed` と `analyzed`）。本番パイプラインが
  載ったとき、どちらへ行くかを決めるのは workflow 定義であり、
  ここを間違えると実績集計から漏れる。Phase 2 でこのADRを見直すこと
- `failure_class` と job status の対応表が**2つの真実になりうる**。
  写像は `domain/job/transitions.py` の `job_event_for_failure()` に1つだけ置き、
  他所で再実装しない規律に依存している（機械検査は未実装）
- Phase 0 の設計書に書いた `attempt` / `stage` を参照した外部メモが陳腐化する
