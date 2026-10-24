# host-process-monitor のテスト設計根拠

`tests/unit/test_host_process_monitor.py`（unit。偽の `/proc` と cgroup を `tmp_path` に作る。実ホストに触れない）。

| テスト | 守るもの / 落ちたら何が起きているか |
|---|---|
| `test_zombies_are_counted_by_parent_and_judged` | zombie を**親ごと**に数え、閾値で warn / crit。ADR-0052 の形（1 つの親に数万）を合計だけで見ると他の少数の zombie に埋もれる |
| `test_comm_with_space_and_paren_is_parsed` | comm に空白・括弧があっても親 pid / state を取り違えない（`Web Content` 等。取り違えると zombie の数が合わない） |
| `test_process_vanishing_or_unreadable_is_skipped_not_fatal` | 読む前に消えた process・壊れた stat で監視が落ちない（常にある競合） |
| `test_cgroup_pids_ratio` / `test_cgroup_root_missing_is_unknown_not_ok` | `pids.max=max`（無制限）は比率を出さない。cgroup が読めないとき ok と偽らず unknown（監視できていないことが見える） |
| `test_the_monitor_never_signals_or_writes_outside_its_state_dir` | kill・signal・subprocess・docker 停止の文字列が script に無い。**読み取り専用**の契約を壊す変更を落とす |
| `test_output_is_quiet_when_ok_and_unchanged_and_loud_on_change` / `test_corrupt_state_file_is_treated_as_first_run` | 1 分毎に動かしても journal を埋めない（変化と heartbeat だけ）。壊れた state でも動き、通知を黙り込まない |
| `test_exit_code_reflects_worst_level` | crit > unknown > warn > ok の順。unknown を 0 にしない |
| `test_main_runs_against_a_fake_host_and_reports_json` | 引数から出力・終了コードまでの一連（top の親の pid・comm・件数が出る） |
