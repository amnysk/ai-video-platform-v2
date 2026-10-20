# release preflight: 各テストが存在する理由

対象: `domain/release_preflight.py` / `infrastructure/release/` / `scripts/release-preflight.py`（ADR-0050, INV-65）。
実測の事故ではなく**設計上の穴**を塞ぐテストなので、各テストが防ぐ誤判定を書く。

| テスト | 防ぐ誤判定 |
|---|---|
| `test_unknown_never_maps_to_ready` | gh / Temporal / DB が落ちている日に「確認できなかった」が READY に丸まる |
| `test_missing_required_check_is_unknown_not_ready` | チェックを足し忘れた／例外で走らなかったのに READY になる |
| `test_blocked_wins_over_unknown` | BLOCKED が UNKNOWN に隠れて「再確認すれば進める」と誤読される |
| `test_exit_codes` | 自動化が 0 以外を区別できない（READY=0 / BLOCKED=2 / UNKNOWN=3 を固定） |
| `test_collector_exception_becomes_unknown` | collector の想定外の例外で preflight 自体が落ち、終了コードが偶然 0/1 になる |
| `test_target_sha_*` / `test_dependent_checks_*` | `HEAD` / 短縮 SHA / ブランチ名で別の commit を deploy する |
| `test_worktree_must_be_clean_and_at_the_sha` | build 対象が target と違う（deploy-workers.sh は HEAD から build する） |
| `test_ancestry_*` / `test_non_ff_*` | 本番より古い／分岐した commit の誤 deploy（rollback は `--allow-non-ff` を明示） |
| `test_ci_*` | gate 以外の「全部 success」・名前違い・失敗・実行中・0 件を成功扱いする（gate 失敗は BLOCKED、gate 不在は UNKNOWN） |
| `test_container_*` | 稼働中の revision 混在（2026-09-20 の事故）と dirty/unknown ビルドの見逃し |
| `test_alembic_prod_must_be_on_the_target_chain` | target が本番の知らない/先の revision に migrate しようとする |
| `test_*migration*` | expand-only の見落とし。downgrade の drop を誤検出しない／既存ファイルを再判定しない／marker は存在する ADR 付きだけ |
| `test_pending_provider_reservations_*` | 未照合の予約を残したまま入れ替え、再送（二重課金）判断を失う（ADR-0013） |
| `test_schedule_snapshot_*` / `test_post_deploy_schedules_*` | deploy 後に Schedule が pause のまま残る（2026-09-19） |
| `test_backup_*` / `test_replay_*` / `test_owner_approval_*` | 他の SHA の証跡の流用、古いバックアップ、承認なしの READY |
| `test_post_deploy_*` | deploy 後に一部だけ新しい版・poller 不在・alembic が head でないのを見逃す |
| `test_release_preflight_read_only.py` | collector に書き込み系の docker/git/SQL/Temporal 呼び出しが混ざる（許可リスト） |
| `test_release_queues.py` | queue 一覧を別に書き直して compose とずれる（AGENTS.md §8） |
| `test_compose_staging.py` | staging が本番の project・volume・bind path・port・namespace・資格情報を共有する |
| `test_release_migration_scan.py` の bypass / UNKNOWN サンプル | `DROP INDEX/CONSTRAINT/TYPE`・`DELETE FROM`・`drop_constraint`・型変更・`server_default=None`・helper 経由・非リテラル SQL・`primary_key=True` が expand-only として素通りする |
| `test_migration_edited_in_place_is_blocked` | 本番に入った過去の migration の中身を書き換えて破壊的変更を忍ばせる（新規ファイルしか見ない走査の穴） |
| `test_marker_*` | 文字列/docstring 内の印、target にだけある ADR、Proposed の ADR、別 revision の ADR による承認のなりすまし |
| `test_every_tee_step_runs_under_pipefail` / `test_every_teed_pytest_log_is_checked_*` | `pytest \| tee` が pytest の失敗を隠して CI が緑になる／0 件で緑になる |
| `test_release_evidence_ignored.py` | `release/` の証跡が未追跡ファイルとして `deploy-workers.sh` の dirty 判定を落とす |
