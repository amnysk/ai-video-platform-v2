# Episode

## 定義

**Episode = 1本の動画作品**。企画されてから、投稿され、実績が回収されるまでの
ライフサイクル全体を指す集約ルート。

Episodeは**長生きする**。工程が失敗しても、原則としてEpisodeは死なない（INV-12）。

## 同一性

`episode_id`。外部からの再実行・再投稿でも同じEpisodeを指す。
Phase 1 の生成は `uuid.uuid4()`（`infrastructure/db/repositories.py`）。
生成順ソート可能な UUIDv7 への移行は Phase 2 の課題。

## 状態

| 状態 | 意味 | terminal |
|---|---|---|
| `planned` | 企画が確定した。まだ何も作っていない | |
| `in_progress` | いずれかの工程が進行中 | |
| `needs_work` | retryable失敗または品質ゲート未達。自動再試行の対象 | |
| `blocked` | 人間の判断待ち（予算・権利・分類不能な失敗） | |
| `ready_for_review` | 成果物が揃い、人間の確認を待つ | |
| `approved` | 投稿してよいと判定された | |
| `uploaded` | private投稿済み。upload workflow が `render_ready` から入り受領 Artifact を保存した（ADR-0020）。再投稿しない | |
| `analyzed` | 実績を回収し、学習へ反映済み | ✔ |
| `completed` | 骨組みworkflowが正常終了した（ADR-0006） | ✔ |
| `script_ready` | 台本が生成・検証され、次工程を待つ（ADR-0011） | |
| `storyboard_ready` | storyboard が生成・検証され、次工程を待つ（ADR-0015） | |
| `assets_ready` | シーン素材とマニフェストが揃い、次工程を待つ（ADR-0017） | |
| `render_ready` | 完成動画が技術検査に合格して保存され、次工程を待つ（ADR-0019） | |
| `failed` | permanent失敗。回復経路なし | ✔ |
| `cancelled` | 所有者が明示的に中止した | ✔ |

**terminalは4つだけ**（`analyzed` / `completed` / `failed` / `cancelled`）。
語彙の権威は `contracts/states.py` の `EpisodeStatus` と
`EPISODE_TERMINAL_STATUSES`。ここと一致していなければならない。
それ以外の状態は必ず自動または人間による出口を持つ。
出口の無い状態を追加してはならない。

## 属性

**Phase 1 で実装済み**（`infrastructure/db/models.py` の `EpisodeRow`）:

- `id`, `created_at`, `updated_at`
- `status`, `status_changed_at`（列名。本文では state と同義）
- `topic`（最大 `contracts.topic.TOPIC_MAX_CHARS` = 200 字。列は migration 0009 で `String(200)`）,
  `workflow_id`（Temporal 参照。**状態の権威ではない** / INV-8）, `blocked_reason`（`blocked` のとき非NULL）
- `topic_plan_id` — 題材を決めた `topic_plans` 行（ADR-0025、一意・NULL 可。Planner 導入前の Episode は NULL）

関連テーブル（ADR-0025）: `topic_plans`（1日・1 profile の組に1つの確定 Topic）、`topic_candidates`
（検討した候補、plan 削除で消える）、`analytics_snapshots`（YouTube Analytics の取得結果）。
Topic の企画は Artifact ではなくこれらの行として残る。

**未実装**（列が存在しない）:
- `title_draft`, `format`（例: `youtube_short`）, `workflow_run_id`
- `retry_budget_used` — 課金を伴う再生成の消費数
- `cost_jpy_committed`, `cost_jpy_reserved`

## 不変条件

- 状態遷移は [state-transitions.md](./state-transitions.md) の表に無いものを行わない
- `blocked` なら `blocked_reason` が必ずある
- terminal状態から他状態へ戻らない（再挑戦は新しいEpisodeを作る）
- `failed` へ落とせるのは failure-policy §2 の3条件を全て満たすときだけ

## Episodeが持たないもの

- 工程の順序（Temporal workflowが持つ）
- 成果物の本体（Artifactが持つ）
- 個々の試行の記録（Jobが持つ）
