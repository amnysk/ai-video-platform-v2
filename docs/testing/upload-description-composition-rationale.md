# テスト設計の根拠: 構成された description（ADR-0075、INV-111 / INV-112）

守るもの: (1) flag OFF の description が現行（台本の全文）から1 byte も変わらない、(2) 構成を使うときに、参照・
クレジットを黙って切らず、内部 ID を出さない。どちらも契約層の純粋関数なので `tests/contract/`（I/O なし）。

## `tests/contract/test_upload_description_golden.py`（INV-111）
期待値は導入前のコードの出力を固定した値（短文・日本語＋`<>`・長文・必須部分・空本文を marker あり / なしで）。
落ちたら現行の description が変わっている。値を書き換えて通すのは禁止（ADR で現行の変更を先に承認する）。
`test_the_composition_flag_is_off_by_default_*` は flag の既定と truthy の判定、
`test_flag_on_without_a_composition_input_*` は flag ON でも入力を渡さない呼び出し（今の Activity）が変わらないこと。

## `tests/contract/test_upload_description_composition.py`（INV-112）
| テスト | なぜ必要か |
|---|---|
| `test_order_is_...` / `test_without_marker_*` / `test_composition_is_deterministic` | 構造（導入→補足→参照→クレジット→marker）と決定性。順序の退行・非決定は同じ入力で別の説明欄を作る |
| `test_credit_lines_are_kept_verbatim` / `test_credit_with_angle_brackets_blocks_*` | 帰属文は一字も変えない（変えるとライセンス条件を満たさない）。`<>` は書き換えず BLOCK |
| `test_required_references_missing_blocks` | 歴史動画で出典が無いまま通さない |
| `test_required_parts_over_the_limit_*` / `test_not_enough_room_for_the_intro_blocks` | 必須部分が入らないときは切らずに BLOCK。導入文が残らない説明欄も通さない |
| `test_long_optional_parts_are_trimmed_but_required_parts_survive` / `test_whole_supplements_*` | 長いときに落とすのは任意の部分だけ。補足は途中で切らずまるごと、後ろから。5000 bytes 以内 |
| `test_internal_ids_in_any_part_block`（4 種） | 導入文・補足・クレジットのどこに内部 ID があっても BLOCK。例外・理由に値を出さない |
| `test_the_final_string_is_linted_too` | 組み上がった文字列にも lint を掛ける（部分ごとの検査の取りこぼしへの備え）。marker 行だけは除く |
| `test_the_marker_line_itself_*` | marker 行自体で誤って BLOCK しない |
| `test_reference_and_composition_shapes_*` | 入力の形（http(s) の URL、1行の題、空文字なし） |
| `test_flag_off_ignores_the_composition` / `test_flag_on_*` | 結線: OFF は無視、ON は全文でなく構成を使い、tags の marker・private・lint・送信直前の lint は変わらない |
| `test_music_credit_text_*` | クレジット行の型が `contracts/music.py` の帰属文（str）と同じ。music 側が型を変えたら気付く |
