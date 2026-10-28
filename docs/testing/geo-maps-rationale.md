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
