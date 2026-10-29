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
