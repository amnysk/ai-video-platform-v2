# テスト設計の根拠: 歴史地図の正確さ・見た目・決定論的描画（ADR-0078、INV-117〜120）

実際の事故から作ったテストではない。設計書 v3 §3.2 / §3.3 の「地図の誤り」を、(1) 歴史的正確さ
（BLOCK し得る）、(2) 見た目の品質（正確さを救えない）、(3) 決定論的描画、(4) flag OFF の不変、に分けて
固定する。DB・Temporal・provider・ネットワークには触れない。fake gazetteer は ADR-0071 のものを再利用する。

## `tests/unit/test_geo_map_accuracy.py`（INV-117）

| テスト | なぜ必要か |
|---|---|
| `test_a_correct_map_passes_with_a_single_ok_finding` | 正しい地図が PASS し、結果が空にならない（空の結果を PASS と読ませない） |
| `test_misplaced_coordinates_are_blocked_with_the_place_as_target` | 位置ずれ（関ヶ原を東京に）が BLOCK で、対象と根拠の参照が結果に載る |
| `test_swapped_axes_are_blocked_with_their_own_reason` | 軸順の取り違えが「位置ずれ」と別の理由コードで見える |
| `test_an_anachronistic_place_name_on_a_historical_map_is_blocked` | 検証器は REVIEW だが、1600 年の地図に Tokyo を出すのは誤った史実の提示。関門では BLOCK |
| `test_an_anachronistic_label_is_blocked` | 地点名が正しくてもラベルだけアナクロなら止める |
| `test_a_place_that_did_not_exist_yet_is_blocked` | まだ無かった城を描く |
| `test_modern_borders_on_a_historical_map_are_blocked` / `…_of_another_era_…` / `test_a_border_claim_without_a_border_basis_…` | 現代国境・年代違いの国境・根拠のない国境の主張 |
| `test_modern_borders_on_a_modern_context_map_are_not_an_accuracy_error` | 引き上げが現代地図の正当な国境まで止めない（範囲を歴史地図に限る） |
| `test_a_place_whose_claim_does_not_resolve_…` / `…source_was_not_fetched_…` / `test_an_unsupported_claim_is_not_evidence` | 根拠に結びつかない地点は PASS にならない（claim 無し・資料未取得・claim が弱い） |
| `test_a_place_without_a_claim_cannot_even_be_built` | 契約が検証器より前に止める層の確認（二重の防御。契約を緩めると落ちる） |
| `test_a_homonym_stays_a_review_and_is_never_auto_picked` / `test_an_id_that_names_the_reference_disambiguates_a_homonym` | 同名異所は自動で 1 つに決めず REVIEW。参照 id で明示すれば通る（曖昧さを解く手段の存在） |
| `test_an_unknown_place_is_a_review_not_a_pass` / `test_an_unreadable_time_basis_is_a_review` | 判断材料が足りないものは BLOCK ではなく REVIEW のまま（過剰な引き上げの防止） |
| `test_a_schematic_without_the_disclaimer_…` / `test_a_labelled_schematic_passes` | 模式図の免責（INV-96）が理由コードで見える。免責があれば通る |
| `test_a_scene_claiming_a_real_location_without_a_manifest_is_blocked` / `test_a_schematic_presented_as_a_real_location_…` / `test_a_scene_without_a_map_claim_passes` | INV-93 が新しい結果の形でも保たれる |
| `test_the_result_is_deterministic_and_carries_no_free_text` | 同じ入力から同じ JSON。finding の項目が固定で、自由文を持たない（LLM / manifest の文字列を流さない） |
| `test_the_result_verdict_cannot_be_milder_than_its_worst_finding` / `test_a_non_pass_finding_needs_a_real_reason_not_ok` | BLOCK を含む結果を PASS として作れない・理由の無い非 PASS を作れない |
| `test_every_non_pass_verifier_check_is_classified` | 検証器に check を足したのに分類表が追随しないと UNCLASSIFIED になる。それを検出する通報 |
| `test_escalation_never_relaxes_any_verdict` | 全ての理由コード × 地図の種類 × verdict で、引き上げが REVIEW → BLOCK の一方向（緩める経路が無い） |

## `tests/unit/test_geo_map_presentation.py`（INV-118）

| テスト | なぜ必要か |
|---|---|
| `test_a_legible_layout_passes_with_one_ok_finding` | 読める地図が PASS し、結果が空にならない |
| `test_overlapping_labels_are_flagged_for_both_targets` / `test_touching_edges_are_not_an_overlap` | 重なりは両方のラベルが対象。辺が接するだけは重なりではない（境界の定義） |
| `test_a_font_below_the_minimum_is_flagged` / `test_a_font_exactly_at_the_minimum_is_accepted` | 最小フォントの境界 |
| `test_low_contrast_is_flagged` / `test_contrast_ratio_matches_the_wcag_definition` | 淡い文字の検出と、比の計算が WCAG の定義（21:1・1:1・対称） |
| `test_a_label_outside_the_canvas_is_flagged` / `test_a_label_touching_the_canvas_edge_is_inside` | 4 辺それぞれのはみ出しと、ちょうど接する場合 |
| `test_a_required_disclaimer_…` / `test_a_disclaimer_off_canvas_or_unreadable_is_blocked` / `test_a_visible_disclaimer_passes` | 模式図の免責が無い・画面外・小さい・薄いは BLOCK（INV-96 の表示） |
| `test_presentation_is_deterministic` | 同じ幾何から同じ結果 |
| `test_presentation_findings_use_presentation_codes_not_accuracy_codes` | 理由コードの語彙を共有しない（見た目と正確さの混同を型で防ぐ） |
| `test_good_presentation_never_upgrades_accuracy` / `test_every_pair_of_verdicts_is_monotone` | 全 verdict の組み合わせで、合流が正確さより緩くならない |
| `test_a_perfectly_legible_inaccurate_map_is_still_blocked` / `test_poor_presentation_alone_does_not_block_an_accurate_map` | 見た目は正確さを救わず、見た目だけで BLOCK にもしない |
| `test_a_combined_result_cannot_be_milder_than_accuracy` / `test_results_for_different_scenes_do_not_combine` / `test_the_combiner_accepts_only_results_never_a_verdict_or_score` | 契約が緩い合流・別シーンの合流・点数の入力を作れない |

## `tests/unit/test_geo_map_render.py` と `tests/architecture/test_geo_maps_boundaries.py`（INV-119）

| テスト | なぜ必要か |
|---|---|
| `test_the_same_manifest_renders_to_identical_bytes` | 再描画で画像が変わると、artifact の sha256・再利用判定が揺れる |
| `test_rendering_in_a_fresh_process_gives_the_same_hash` | 同一プロセス内の偶然の一致ではなく、ハッシュシード・import 順の違う別プロセスでも同じ |
| `test_the_order_of_places_and_labels_does_not_change_the_image` | 入力配列の順（LLM の出力順）が画像に漏れない |
| `test_a_different_manifest_gives_a_different_image` | 何を渡しても同じ画像、という退化を防ぐ |
| `test_the_png_is_a_plain_rgb_image_…` | 時刻等を埋めうるメタデータ chunk が無い |
| `test_the_image_actually_draws_something` / `test_a_marker_lands_where_the_projection_says` | 空画像で決定論が自明に通るのを防ぐ。東が右・北が上で投影の向きを固定 |
| `test_the_font_identity_is_part_of_the_render_identity` | フォントが変われば画像が変わるので同一性の入力に入れる |
| `test_the_layout_of_a_clean_map_passes_the_presentation_check` / `test_crowded_labels_are_reported_…` | 描画器が返す幾何が見た目の検査に実際に使える。重なりを隠さず報告する |
| `test_a_schematic_draws_its_disclaimer_…` / `test_a_schematic_without_a_disclaimer_is_blocked_…` | 模式図の免責が画像に描かれ、無ければ見た目の検査が BLOCK |
| `test_a_font_must_be_supplied_as_bytes_…` | 壊れたフォントで黙って既定フォントに落ちない |
| `test_rendering_makes_no_network_or_fal_calls` / `test_a_fresh_process_…_never_loads_a_provider_module` | fal 呼び出し 0（socket を spy し、provider の module を import しない） |
| `test_the_clip_argv_…` | 既存 render 線の fps・収め方・bitexact を再利用した静止画→mp4 の argv が決定論的 |
| `test_map_modules_import_no_paid_provider_network_clock_or_random` / `test_the_detector_catches_violating_fixtures` / `test_the_map_modules_do_not_call_…` | INV-18 型の構造的保証。検出器自身が違反 fixture を検出できる |
| `test_production_side_does_not_import_the_map_parts` | 地図の部品を本番側が import しない（未接続・INV-120 の前提） |

## `tests/unit/test_geo_maps_flag_off.py`（INV-120）

| テスト | なぜ必要か |
|---|---|
| `test_the_flag_defaults_to_off_and_is_read_from_the_environment` | 既定 OFF と環境変数名 `GEO_MAPS_ENABLED`（Settings の既存慣習）の確認。既定が ON に変わる事故を止める |
| `test_with_the_flag_off_nothing_is_rendered` | OFF で描画関数が呼ばれない（spy で 0 回） |
| `test_with_the_flag_on_a_map_is_rendered` | OFF が常に None を返すだけの退化でない |
| `test_render_identity_and_filtergraph_are_byte_identical_to_before` | golden: 接続前の系統（6d14c30 / audit 枝）で計算した `render_plan_sha256` と filtergraph の sha256。ffmpeg_graph の `fit_filter` 改名など本枝の変更が既存の出力を変えていない証明（2 profile） |

## グリフ欠落（独立レビュー MAJOR 1。INV-118 / INV-119）

| テスト | なぜ必要か |
|---|---|
| `test_a_non_latin_label_without_a_capable_font_is_not_a_pass` | 既定フォント（ASCII のみ）で「関ヶ原」を描くと豆腐になるが、箱の大きさは変わらず従来は PASS だった。描画器が欠落を申告し、見た目の検査が BLOCK |
| `test_ascii_labels_are_not_reported_as_missing_glyphs` | 検出が全ラベルを欠落扱いする過剰検出の防止 |
| `test_a_disclaimer_with_missing_glyphs_is_blocked` | 模式図の免責が豆腐では INV-96 の表示要件を満たさない |

## 境界検査の import 形（独立レビュー MAJOR 2。`test_geo_maps_boundaries.py`）

| テスト | なぜ必要か |
|---|---|
| `test_the_detector_sees_from_package_import_submodule` | `from infrastructure import providers` は `node.module` だけ見ると `infrastructure` で素通りする。alias を `a.b` として足す |
| `test_the_detector_resolves_relative_imports` | `from ..providers import x` を絶対名に直さないと見えない |
| `test_the_detector_sees_dynamic_imports` | `importlib.import_module("...")` / `__import__("...")` の定数引数。地図の部品では `importlib` 自体も禁止 |
| `test_the_production_side_scan_sees_the_map_parts_in_every_import_form` | 本番側が `from infrastructure.media import map_render` 等でも地図の部品を import したと検出できる |

## 免責との重なり（独立レビュー MINOR 3）

| テスト | なぜ必要か |
|---|---|
| `test_a_label_covering_the_disclaimer_is_an_overlap` | 免責の板を重なり検査から外すと、ラベルが免責を隠しても PASS になる |
