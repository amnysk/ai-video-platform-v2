# opensearch-guard（メモリ保護・自動復旧・警報）のテスト設計根拠

ADR-0059 / INV-79・INV-80。対象は `scripts/opensearch-guard.py` と `scripts/avp_alerts.py`。
実ホストの docker・systemd・ネットワークには一切触れない。偽の `/proc/meminfo`・**偽の docker CLI**
（argv を記録する実行ファイル。guard は本物と同じく subprocess で呼ぶ）・偽の時計（`main(now=)`）だけを使う。

## 背景の事故

2026-10-21 00:02、`check-pipeline.sh --enforce-memory` が **1 サンプル**（1039MiB）で OpenSearch を止め、
5 分後には 5487MiB に戻っていたのに誰も起こさず 4 日止まった。その間 Fluent Bit が 105 chunk を破棄した。
下の各テストは、この事故の別々の面を再発させない。

## `tests/unit/test_opensearch_guard.py`

| テスト | 守るもの / 落ちたら何が起きているか |
|---|---|
| `test_transient_dip_below_threshold_stops_nothing`（**要求 test 11**） | 1 サンプルの低下で止めない。落ちたら 10-21 の事故が再発する |
| `test_oscillating_memory_never_starts_a_stop_start_loop` / `test_a_long_gap_between_samples_breaks_the_low_run` | 低下と回復の往復で止めない（停止/起動のループ無し）。timer が止まっていた間の古い値を「連続」に数えない |
| `test_sustained_pressure_still_stops_opensearch_and_dashboards_only` / `test_the_stop_needs_n_samples_and_minimum_duration` | **保護の目的は残す**。連続 5 サンプルかつ 180 秒で止める。止めるのは avp2-logging の opensearch と dashboards だけ（temporal・postgres・worker は running のまま） |
| `test_opensearch_stop_raises_alert_records_state_and_spares_others`（**要求 test 10**） | 停止で crit 警報が出て、理由・停止時刻・サンプル・復旧条件・試行回数が state に残り、他は無傷 |
| `test_alert_is_written_to_stderr_with_a_journal_priority` / `test_opensearch_stopped_longer_than_threshold_alerts` | 警報が OpenSearch を経由せず journal の priority 付きで出る。停止が X 秒を超えたら別の警報（4 日誰も気付かなかった） |
| `test_dry_run_changes_nothing` / `test_unreadable_meminfo_neither_stops_nor_claims_ok` | 読めない・試運転のとき止めない。「読めなかった」を OK にしない |
| `test_auto_restart_is_off_unless_the_flag_is_given` | 自動起動は opt-in。導入は所有者の判断 |
| `test_auto_restart_starts_only_the_opensearch_container` | `docker start <その 1 コンテナ>` だけ。compose / rm / run / restart を呼ばない |
| `test_restart_waits_for_the_restart_line` / `test_restart_needs_three_consecutive_samples_above_the_line` / `test_a_dip_inside_the_restart_run_resets_the_count` / `test_min_stopped_time_prevents_an_immediate_bounce` | 復旧線（4096MiB x 連続 3）は停止線（1.5GiB）より明確に高い。途中の低下で数え直す。直後の再起動をしない |
| `test_restart_requires_all_workers_healthy`（4 形）/ `test_one_shot_exited_zero_container_does_not_block_restart` | unhealthy・starting・restarting・異常終了の worker があれば起こさない。正常終了した one-shot は妨げない |
| `test_never_restarts_an_opensearch_the_guard_did_not_stop` | 人や古い timer が止めたものを勝手に起こさない（警報だけ）。**現在の 4 日停止もこの形** |
| `test_flap_cap_stays_stopped_and_alerts_after_max_restarts` / `test_flap_cap_is_released_after_24_hours` / `test_backoff_doubles_between_attempts` | 24 時間の自動起動に上限。上限後は止めたまま crit 警報。backoff は倍々。24 時間後に解除 |
| `test_buffer_near_its_limit_alerts_before_any_drop` / `test_oldest_chunk_nearing_the_retry_window_alerts_before_drops` / `test_dropped_chunks_in_fluent_bit_log_raise_a_crit_alert` / `test_fluent_bit_not_running_is_alerted` | 破棄の**前**（buffer 70% / 90%、chunk 年齢が retry 窓の 50% / 80%）と破棄の**後**（`cannot be retried`、chunk id で重複を数えない）の両方を知らせる |
| `test_heartbeat_is_written_every_run_even_when_everything_is_fine` | heartbeat は常に出る（INV-80 の生存証拠） |
| `test_refuses_to_target_the_application_project` / `test_guard_source_never_uses_compose_or_destructive_docker_verbs` | アプリの project を止める誤用を拒む。使う docker の動詞は ps / inspect / stop / start / logs だけ |
| `test_guard_constants_match_the_fluent_bit_config_and_documented_conditions` | guard が持つ retry_limit・scheduler.cap・buffer 上限・復旧線が Fluent Bit の設定・compose・文書の数と一致（AGENTS §8。片側だけの変更を落とす） |

## `tests/unit/test_avp_alerts.py`（alerts）

| テスト | 守るもの |
|---|---|
| `test_same_alert_is_not_repeated_until_the_interval_and_resolves_once` | 30 秒毎に動かしても journal を埋めない。消えたら resolved を 1 回 |
| `test_a_level_change_is_emitted_immediately_and_sources_are_independent` | warn から crit への悪化は間隔を待たず出る。source ごとに独立 |
| `test_heartbeat_roundtrip_and_missing_or_corrupt_is_none` / `test_unwritable_state_dir_never_raises` | 無い・壊れた heartbeat は「生きている証拠が無い」。書けなくても監視は落ちず journal には出る |
