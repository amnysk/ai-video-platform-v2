# backup / restore drill: 各テストが存在する理由

対象: `domain/backup/` / `infrastructure/backup/` / `scripts/backup.py`（ADR-0051, INV-66）。
実測の事故ではなく**設計上の穴**を塞ぐテスト。本番・docker・実 MinIO には触れない（fake runner）。

| テスト | 防ぐ誤り |
|---|---|
| `test_a_new_manifest_is_valid_and_never_restore_verified` / `test_validation_rejects_*` | 取っただけの backup が「復旧できる」と読まれる／証跡なしの `restore_verified: true` |
| `test_only_a_fully_passing_drill_flips_restore_verified` / `test_a_failed_or_missing_check_*` / `test_evidence_for_another_backup_is_rejected` | 一部の検査だけ pass、検査の欠落、別 backup の証跡で検証済みになる |
| `test_row_counts_must_fall_inside_the_backup_window` | 復旧で行が欠ける・余計な表がある（書き込み中の取得は許容し、欠落は検出） |
| `test_reference_integrity_flags_*` | DB 行が指す本体が無い／sha256 が違う／backup 対象外の bucket を指す（PG↔MinIO の食い違い） |
| `test_restore_refuses_any_production_identifier` / `test_restore_refuses_a_production_target_before_running_anything` / `test_restore_refuses_the_backup_root_*` | 復旧が本番の container・project・host・port・DB・bucket・データ領域・backup root に向く（1 つの設定誤りで本番を壊す）。docker を 1 度も呼ばず manifest も触らないことまで固定 |
| `test_destination_on_the_production_device_is_refused` / `test_same_device_can_be_allowed_*` | backup が守る対象と同じディスクに置かれ、故障で両方失う（許す場合は manifest に残る） |
| `test_unsafe_object_keys_never_escape_the_backup_directory` / `test_unsafe_object_keys_are_not_turned_into_paths` | `../` 等の key で backup 外へ書く |
| `test_pg_dump_failure_exits_*` / `test_empty_or_unreadable_dump_*` | 失敗した dump が「成功した backup」として残る。非ゼロ・audit 行・通知・partial 掃除まで固定 |
| `test_a_migration_during_the_backup_fails_it` | migration 中に取った、スキーマの食い違う dump |
| `test_size_mismatch_and_etag_mismatch_are_detected` | 転送中の欠損（checksum mismatch）を検出せず確定する |
| `test_a_failing_notifier_does_not_hide_the_backup_failure` | webhook の失敗で本来の失敗が握りつぶされる |
| `test_unchanged_objects_are_hardlinked_*` / `test_a_changed_object_is_downloaded_again` | 増分の取りこぼし（変わった本体を共有してしまう）／毎回全量で容量が増える |
| `test_files_are_private_to_the_owner` | dump（個人/運用データ、ロールのハッシュ）が他ユーザーに読める／共有 hardlink の本体を誤って書き換えられる |
| `test_offsite_is_off_by_default_and_a_failing_offsite_*` | 設定していないのに外部へ送る／offsite 失敗で成功扱い |
| `test_a_corrupted_backup_file_is_detected_and_nothing_is_restored` / `test_a_missing_file_is_a_failure` | bit-rot・欠落を見逃す／壊れた写しから復旧を試みて「通った」ように見える |
| `test_a_failed_drill_revokes_a_previous_verification` | 以前の検証済みが、後に壊れた後も「検証済み」のまま残る |
| `test_row_loss_*` / `test_alembic_revision_mismatch_*` / `test_a_db_row_pointing_at_*` / `test_a_pg_restore_failure_*` / `test_a_container_that_cannot_start_*` | 各検査が実際に落ちること、失敗時も片付けること |
| `test_unknown_or_inconsistent_manifests_abort_without_evidence` | 2 つの root の manifest が食い違うのに演習する |
| `test_retention_never_deletes_unverified_or_incomplete` / `..._the_last_verified_backup` / `test_retention_deletes_verified_*` / `test_retention_keeps_the_newest_of_each_*` | 未検証・最後の検証済みの削除（復旧手段の喪失）／方針の日・週・月の取り違え |
| `test_prune.py` 全般 | 計画後に検証が取り消された集合の削除、片方の root だけの検証、`backup_id` 形でない/ symlink のディレクトリの削除 |
| `test_cli.py` | 保存先の既定が無いこと、本番の作業領域を拒否、証明できないときは fail-closed（docker inspect 失敗で実行しない）、dry-run が既定 |
| `test_backup_contracts.py` | 識別子が compose.yaml とずれる／manifest が release preflight の `backup_freshness` で読めなくなる（stale は BLOCKED） |
| `test_backup_deploy_examples.py` | 例が勝手に install される／既定で外部送信・通知が有効になる |
| `test_backup_boundaries.py` | backup が本番へ書き込み系を送る／drill 以外が `restore_verified=True` を書く／削除や `docker run/rm` の置き場が増える／想定外の外部通信 |
| `test_guard_overlap.py` | backup root が MinIO データ dir の中/外側/symlink/`..` 経由で重なる（normpath だけでは見逃す）。`--allow-same-device` でも緩めないこと、restore の work_dir が symlink で保護パスへ向くこと |
