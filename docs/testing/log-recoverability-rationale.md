# log-recoverability のテスト設計根拠

ADR-0059。`scripts/log-recoverability.py`（読み取り専用）。4 日の OpenSearch 停止で、どのコンテナのログが
どこまで json-file から取り戻せるか（temporal-1 は 10-20T15:02Z から 10-24T13:41Z が rotation で消えた）を
**推測でなく機械で**出すため。偽の docker（ps / inspect のみ）と tmp の containers dir を使う。

| テスト（`tests/unit/test_log_recoverability.py`） | 守るもの |
|---|---|
| `test_rotated_away_start_of_the_window_is_unrecoverable` | rotation で消えた窓の先頭を unrecoverable、残りを recoverable に分ける。ナノ秒の時刻を読める |
| `test_fully_retained_window_exits_zero` | 全部残っていれば終了コード 0（失われた範囲を捏造しない） |
| `test_rotated_older_files_are_read` | `.log.1` 等の旧ファイルも読む（読まないと残っているログを失われたと誤報する） |
| `test_container_without_any_log_file_is_wholly_unrecoverable` | ログが 1 行も無いコンテナを「問題なし」にしない |
| `test_window_starts_at_container_creation` | コンテナ作成前のログは存在しないので失われた範囲に数えない |
| `test_backfill_copies_only_window_lines_verbatim_and_never_writes_the_source` | backfill は窓の行だけを原文のまま複写（0600）。元の json-file は無変更（ハッシュ一致） |
| `test_report_only_mode_writes_no_files` / `test_out_inside_the_containers_dir_is_refused` | `--out` 無しでは何も書かない。元のログの dir 内への出力を拒む |
| `test_malformed_lines_are_counted_not_fatal` | 壊れた行（rotation 途中の欠け等）で止まらず、件数を出す |
| `test_source_only_uses_read_only_docker_verbs` | stop・start・rm・compose・network が無い |
