# CI 方針の機械検査のテストの理由

ADR-0049。`ci.yml` は branch protection の required check の元になるので、片側（job だけ／gate だけ）の更新を
機械で止める。

- `tests/contract/test_ci_workflow_policy.py`
  - `permissions` が `contents: read` のみ: 既定の GITHUB_TOKEN は書き込み権限を持ちうる。
  - push が `main` / `develop` / `release/**` / `hotfix/**` を覆い、PR は base を絞らない: 以前は
    `push: [main]` で、`main` が実在しない間、統合ブランチへの push で CI が一度も走らなかった実害がある。
  - required check 名の安定: 名前の変更は branch protection の設定変更と同義。
  - `CI gate` の `needs` が他の全 job と一致: job を足して gate に入れ忘れると、その job は誰も見ない。
    失敗を先に確認した（gate 追加前は 5 件失敗）。
  - gate が `always()` で success 以外（skipped 含む）を落とす: `needs` が skipped のとき job 自体が
    スキップされ「緑」になる GitHub の挙動を避けるため。
  - 何もしない job（`true` のみ、`|| true`）の禁止。
- `tests/contract/test_migration_single_head.py`: 並行ブランチが migration の head を2本に割る事故を PR で検出する。
  既存 migration は1 head なので最初から緑（新規の失敗を伴わないガードで、head を2本にする変異で赤になる想定）。

## test_release_preflight_cli の時刻固定（時限爆弾の除去）

`tests/unit/test_release_preflight_cli.py` の READY/BLOCKED 系は、`Fake` の証跡時刻を
`NOW = 2026-10-20 12:00 UTC` に固定している一方、CLI は `datetime.now(UTC)` を使っていた。
実時刻が 72h（証跡の最大の古さ）を過ぎた時点で backup / replay 証跡 / 承認が stale 扱いとなり
READY を期待する 2 テストが落ちる（実装のバグではなくテストの時刻依存）。
`main(..., now=)` を注入点として足し、テストは `NOW` を渡す。判定ロジック・閾値は変えていない。

## Temporal replay に本番の watchdog 履歴を足した理由（`tests/unit/test_daily_watchdog_replay.py`）
CI の replay job は、記録済みの履歴が DailyEpisode（0025 前）と Script（OFF 経路）の fixture しか無く、
**毎時走る DailyWatchdogWorkflow は履歴 replay の対象外**だった。ADR-0054（#10）が触る workflow コードは
まさにこの watchdog（Activity 入力への項目追加）で、デプロイ時に進行中の watchdog 履歴が新コードで replay される。
- `test_recorded_production_watchdog_history_replays_without_nondeterminism`: 本番（80775c0）で記録した Completed の
  実履歴を現在のコードで replay する。Activity の順序・数の変更を CI で止める。
- `test_the_fixture_is_a_real_completed_run_with_the_watchdog_activity`: fixture が空・別 workflow に差し替わって
  replay が何も検査しなくなるのを防ぐ。
- `test_a_changed_activity_order_would_be_caught_by_this_replay`: 履歴の Activity 名を書き換えると落ちる = replay が
  実際に履歴と突き合わせている（何も検査しない replay との区別）。
fixture の内容は運用の設定値と件数のみ（URL・秘密・メディア無し）で、取得時に確認した。
