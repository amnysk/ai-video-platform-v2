# v3 Phase 3 wiring（ADR-0086）: テストを置いた理由

各テストが**何の失敗を防ぐか**を残す。ADR-0086・INV-146〜148。

## tests/unit/test_render_usage_declarations.py（render の使用宣言・発話余裕の実測）

| テスト | 防ぐ失敗 |
|---|---|
| `test_without_a_writer_nothing_is_declared` | flag OFF（writer なし）でも宣言の行が書かれ、OFF の「何も書かない」(INV-130) と結線前の同一性が崩れる |
| `test_with_a_writer_but_no_bgm_or_map_both_are_declared_not_used` | 不使用の宣言が無く、BGM・地図の無い普通の動画が全件 REVIEW（USAGE_DECLARATION_MISSING）で止まる。対象 hash・入力 hash が現行の行と食い違う |
| `test_bgm_used_is_declared_with_the_digest_of_the_evidence_at_the_binding_key` | 使ったのに宣言が無い / evidence_ref が実際の証跡と無関係 / 証跡が結び付け key に無く gate が引けない |
| `test_a_map_is_declared_used_even_when_it_was_refused` | 拒否された地図を「使っていない」と宣言し、gate が検査せず BLOCK に届かない（誤った地図が通る） |
| `test_geo_on_without_specs_declares_map_not_used` | 地図の指定が無いのに used と宣言し、証跡の無い検査で止まる |
| `test_a_failing_declaration_does_not_fail_the_render` | 宣言の DB 障害で描画（有料でない高コストな処理）が失敗扱いになる。例外の本文がログに出る |
| `test_the_declaration_is_visible_to_the_gate_repository` | render が書いた宣言を gate の読み口が同じ対象で引けない（hash の取り違え） |
| `test_speech_margin_is_measured_and_stored_in_the_evidence` | 実測が証跡に入らず、全 BGM 動画が永久に REVIEW |
| `test_a_unmeasurable_margin_keeps_the_render_and_leaves_the_evidence_unmeasured` | 測定の失敗で BGM 入りの動画が作れなくなる / 失敗を合格として書く |
| `test_too_loud_music_is_stored_as_a_failed_margin` | 声が BGM に埋もれる動画が不合格として記録されない |
| `test_no_speech_probe_or_no_bgm_means_no_measurement` | BGM 無しの描画で測定器が走る（コストと同一性） |

## tests/unit/test_quality_wiring_gate.py（実の producer と実の gate を render の出力につなぐ）

| テスト | 防ぐ失敗 |
|---|---|
| `test_with_everything_on_and_fresh_records_the_episode_is_admitted` | 全 flag ON で、正しい BGM・地図の動画が配線の食い違い（hash の取り違え・key 不一致）で永久に止まる |
| `test_a_video_without_bgm_or_map_is_admitted_by_the_declared_non_use` | BGM・地図の無い普通の動画が producer を要求されて止まる |
| `test_bgm_used_but_its_check_cannot_be_recorded_is_review` | 証跡が消えた BGM 動画が PASS になる（記録が無いのに通る） |
| `test_a_missing_catalog_means_no_bgm_record_and_review` | カタログが無い環境（API 等）で空の表と照合して誤った BLOCK / PASS を記録する |
| `test_a_license_revoked_after_rendering_is_blocked` | 描画後に失効・取消された曲の動画が通る |
| `test_an_unmeasured_speech_margin_is_review_not_pass` | 声の余裕が未測定のまま PASS になる |
| `test_narration_buried_under_bgm_is_blocked` | 声が BGM に埋もれた動画が通る |
| `test_a_refused_map_stops_the_upload` | 拒否された地図（位置違い・時代錯誤）の動画が通る |
| `test_a_declared_map_whose_evidence_is_gone_is_review` | 地図の証跡が無いのに「検査済み」として通る |
| `test_a_re_rendered_video_does_not_inherit_the_old_declarations_or_records` | 再描画した別 hash の動画が古い PASS を引き継ぐ（INV-126 の配線後の形） |
| `test_the_gate_reads_the_declaration_the_render_wrote` | render と gate が別の対象 hash で宣言を読み書きしている |
| `test_both_branch_producers_are_registered` | producer が登録から落ちて全件 REVIEW に戻る（旧 `test_no_branch_producers_are_registered_yet` の置き換え。旧テストは「空の登録」を固定していた） |
| `test_credit_lines_are_not_read_while_the_description_flag_is_off` | flag OFF で MinIO を読み、従来と挙動が変わる |
| `test_credit_lines_come_from_the_evidence_when_the_flag_is_on` / `test_no_bgm_means_no_credit_lines` | クレジットが説明欄に渡らない / BGM 無しで誤ったクレジットが付く |
| `test_an_unreadable_evidence_is_an_error_not_an_empty_credit` | 壊れた証跡のとき、クレジット無しで黙って投稿される |
| `test_the_linted_description_equals_the_uploaded_one` | lint した説明欄と投稿した説明欄が食い違う（片方だけ credits を渡す） |

## tests/unit/test_review_p3_unwired_quality.py（独立レビューの証拠。d6df797 を取り込み）

| テスト | 防ぐ失敗 |
|---|---|
| `test_review_production_gate_has_bgm_and_map_producers` | 本番の gate に BGM / 地図の producer が無い（B1）。登録の完全な一覧を固定する |
| `test_review_render_declares_usage_for_the_gate` | render が宣言を書かず gate が全件 REVIEW（B2）。writer を渡す前提に合わせて編集 |

## tests/contract/test_migration_0019_quality.py・tests/integration/test_quality_records_postgres.py（ADR-0086 §6）

| テスト | 防ぐ失敗 |
|---|---|
| `test_every_table_rejects_delete_and_truncate_too` | 監査記録を DELETE / TRUNCATE で消せる |
| `test_episode_foreign_keys_restrict_and_the_approval_fk_includes_the_episode` | episode の削除に記録が巻き込まれる / 別 episode の報告を承認できる |
| `test_release_preflight_scanner_finds_only_the_truncate_guard_triggers_in_0019` | 走査器が TRUNCATE 拒否トリガ以外の文を拾う / marker が無効 |
| `test_delete_and_truncate_are_refused_on_every_table`（実 PG） | 上の契約が実 DB で効いていない |
| `test_an_approval_cannot_point_at_another_episodes_report`（実 PG） | 複合 FK が実 DB で効いていない |
| `test_the_wired_gate_reads_real_postgres_records`（実 PG） | SQLite では通るが実 Postgres の記録・宣言の読み書きで配線が崩れる |
