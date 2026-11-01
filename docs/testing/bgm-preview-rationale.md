# BGM 試聴ツールのテスト根拠（tests/unit/test_bgm_preview.py）

試聴ツールは「未承認の曲を鳴らせる」唯一の場所なので、**漏れ出さないこと**と**本番を緩めないこと**を固定する。

| テスト | 防ぐ失敗 |
|---|---|
| `test_production_path_still_rejects_an_unapproved_track` | 試聴対応のついでに `resolve_bgm` / `plan_cue_sheet` が未承認曲を通すようになる |
| `test_preview_override_*` | 上書きが元の曲オブジェクトを書き換える / 承認済みの曲まで変える / 複製が本番の検査を通らない（試聴が黙って BGM 無しになる） |
| `test_unapproved_track_needs_the_flag_and_the_source_catalog_is_untouched` | フラグ無しで未承認曲が通る / 元のカタログが書き換わる |
| `test_tampered_audio_and_unknown_track_are_refused` | 改変・取り違えた音源で試聴して結論を誤る |
| `test_offline_guard_*` / `test_paid_path_modules_loaded_is_a_failure` / `test_importing_the_tool_*` | 試聴が通信・fal・production worker を触る（課金・本番への副作用） |
| `test_watermark_and_phone_simulation_on_a_tiny_clip` / `test_every_output_name_*` | 試聴用の動画が透かし無しで出て、アップロード用と取り違えられる |
| `test_output_dir_may_not_overlap_*` | 出力が export 元・MinIO データ領域を壊す |
| `test_export_function_has_no_write_calls` / `test_export_reads_current_artifacts_and_never_writes` | 読み取り専用のはずの export が本番へ書く（store の put を例外にした fake で確認） |
| `test_offsets_*` / `test_roles_*` / `test_render_requires_a_track_and_out` / `test_main_returns_2_*` | 引数の誤りが黙って別の設定になる |
| `test_shift_levels_*` / `test_sync_check_flags_drift` | 掃引が契約の範囲を超える / 同期ズレを OK と報告する |

ffmpeg が要るテストは固定版が無い環境で skip。実素材の描画（約 1 分/variant）は単体テストに入れず、
`docs/operations/bgm-preview.md` の手順で手動実行する。
