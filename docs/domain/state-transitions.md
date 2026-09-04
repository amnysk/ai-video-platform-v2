# 状態遷移

Episode状態機械の**唯一の権威**。コード側は `domain/episode/transitions.py`
（Phase 1で実装）に1つだけ表を持ち、ここと同じ内容にする。
別モジュールで同じ遷移を書き直さない（AGENTS.md §8）。

## 遷移表

| From | To | 契機 | 主体 |
|---|---|---|---|
| （なし） | `planned` | 企画Artifact生成 | planning worker |
| `planned` | `in_progress` | 制作workflow開始 | workflow |
| `in_progress` | `in_progress` | 工程成功、次工程へ | workflow |
| `in_progress` | `needs_work` | `retryable` 失敗 / 品質ゲート未達 | workflow |
| `in_progress` | `blocked` | `needs_input` 失敗 / 分類不能 | workflow |
| `in_progress` | `failed` | `permanent` 失敗 | workflow |
| `in_progress` | `ready_for_review` | 全成果物が揃った | workflow |
| `needs_work` | `in_progress` | 自動再試行が枠内 | workflow |
| `needs_work` | `blocked` | 再生成の枠を使い切った | workflow |
| `blocked` | `in_progress` | 人間が再開をsignal | API (人間) |
| `blocked` | `cancelled` | 人間が中止 | API (人間) |
| `ready_for_review` | `approved` | 承認 | API (人間 or 自動ゲート) |
| `ready_for_review` | `needs_work` | 差し戻し | API (人間) |
| `approved` | `uploaded` | private投稿成功 | upload worker |
| `approved` | `blocked` | 投稿が `needs_input` 失敗 | workflow |
| `uploaded` | `analyzed` | 実績回収完了 | analytics worker |
| 任意の非terminal | `cancelled` | 所有者の明示的中止 | API (人間) |

**この表に無い遷移は行わない。**

## terminal状態

`analyzed` / `failed` / `cancelled`。ここから出る遷移は無い。
再挑戦は新しいEpisodeを作る。

## 出口の保証

非terminal状態はすべて自動または人間の出口を持つ:

- `planned` → workflow起動（自動）
- `in_progress` → 成功/失敗いずれでも遷移（自動）
- `needs_work` → 再試行 or 枠切れで `blocked`（自動）
- `blocked` → 人間のsignal（**通報される**。放置されない）
- `ready_for_review` → 人間の承認/差し戻し（通報される）
- `approved` → 投稿（自動）
- `uploaded` → 成熟後に実績回収（自動、timer）

**出口の無い状態を追加してはならない。** 追加時はこの節に出口を書く。

## 遷移の実装規則

1. 遷移関数は純粋関数 `transition(current, event) -> next | Rejected`
2. 不正な遷移は例外ではなく `Rejected` を返し、呼び出し側が失敗クラスを決める
3. 遷移とDB書き込みは同一トランザクション
4. 遷移のたびに `state_changed_at` を更新する（stalled検知の材料）

## テスト

- 表の全行を網羅するテスト（`tests/unit/test_state_transitions.py`、Phase 1）
- 表に無い組み合わせが全て `Rejected` になることの網羅テスト
- 全非terminal状態から少なくとも1つのterminalへ到達可能であることの探索テスト
