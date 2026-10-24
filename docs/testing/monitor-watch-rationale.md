# monitor-watch（dead-man's switch）のテスト設計根拠

ADR-0059 / INV-80。`scripts/monitor-watch.py` は監視される側と**別の timer**で動き、heartbeat の古さを見る。
2026-10-21 からの 4 日間、`avp-logging-check` は毎回 exit 1（unit failed）だったが誰も見ていなかった。
「監視が動いていること」を別の目で見る。

| テスト（`tests/unit/test_monitor_watch.py`） | 守るもの |
|---|---|
| `test_fresh_heartbeats_raise_nothing_and_the_watcher_writes_its_own` | 正常時に黙る。watcher 自身も heartbeat を書く |
| `test_a_stopped_guard_is_flagged_while_the_other_monitor_stays_quiet` | guard だけが止まったとき、guard だけを crit にする（他を巻き込まない） |
| `test_a_missing_heartbeat_is_critical_not_ok` | 無い heartbeat は OK ではなく crit（一度も動いていない・壊れている） |
| `test_the_alert_resolves_when_the_heartbeat_returns` | 回復で警報が消え、終了コードが 0 に戻る |
| `test_show_alerts_lists_every_source_and_reflects_severity` | 全 source の有効な警報を 1 回で一覧でき、終了コードが重大度を表す |
| `test_guard_and_host_monitor_share_one_heartbeat_and_alert_directory` | guard・host monitor・watcher が同じ dir で噛み合う（片側だけ止まれば片側だけ警報） |
| `test_watcher_never_touches_docker_network_or_other_processes` | watcher は読み取り専用（docker・network・signal・subprocess が無い） |
