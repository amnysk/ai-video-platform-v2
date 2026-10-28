# テスト設計の根拠: 地図・地理の契約と決定論的検証（ADR-0071、INV-93〜96）

各テストが**なぜ必要なのか**を残す。守るものは 4 つ。

1. 座標の軸順・範囲・CRS の取り違えが契約で作れない（INV-95、T09）。
2. 位置・時代・ラベル・模式図の誤りを、LLM も課金も使わず決定論的に検出する（T07 / T08 / T10 / T11）。
3. 決定論的な BLOCK / REVIEW は LLM の評価で PASS にならない（INV-94、T32）。
4. 検証器は I/O を持たず、本番の課金コードから使われない（INV-6 の追加の枠、ADR-0071）。

実際に起きた事故から作ったテストではない。設計書 v3 のテスト計画（T07〜T11・T32）が想定する事故を
fake gazetteer（メモリ上の固定データ。ネットワークに出ない。検証器の追加と同時に置く）で再現する。

## `tests/contract/test_geo_contracts.py`（契約）

| テスト | なぜ必要か / 落ちたら何が起きているか |
|---|---|
| `test_a_well_formed_manifest_round_trips` | 生成側と取り込み側が同じ契約を通る。片側だけ変わると保存物が読めない |
| `test_coordinates_outside_epsg4326_ranges_are_rejected` | 日本の経度（136°）を緯度に入れる等の軸順の取り違えは、ほとんどが範囲外になる。NaN / inf も通さない（INV-95、T09 の契約側） |
| `test_coordinates_are_named_fields_not_a_positional_pair` | `[lon, lat]` / `[lat, lon]` の順序が文脈頼みの形を受け取ると取り違えが黙って通る |
| `test_only_epsg4326_is_accepted` | 別の CRS の数値を度として読むと位置が大きくずれる |
| `test_bbox_is_named_ordered_and_in_range` | bbox の `[min_lon, min_lat, ...]` の順序取り違えと反転した範囲を作らない |
| `test_valid_years_must_be_ordered` | 存在期間の逆転は時間検査を無意味にする |
| `test_a_real_map_place_needs_a_claim_and_sources` | 実在の位置を主張する点が claim・資料に紐づかないまま描かれるのを防ぐ（紐づきが支持されているかは検証器） |
| `test_claim_and_source_ids_use_the_research_id_patterns` | research の ID 形式を再定義せず同じパターンを共有する（AGENTS.md §8） |
| `test_duplicate_place_ids_are_rejected` | ラベルが指す地点が一意に決まらなくなる |
| `test_unknown_keys_are_rejected` | 綴り違いの項目が黙って捨てられない |
| `test_map_kind_is_a_closed_set` | 想定外の種類が schematic の免責検査を迂回しない |
| `test_a_schematic_manifest_without_a_disclaimer_is_constructible_for_the_verifier` | 免責欠落を構築時エラーではなく BLOCK として記録に残す設計（ADR-0071）。構築時に弾く設計へ変えるなら ADR ごと変える |
| `test_border_basis_year_is_required_iff_historical` | 当時の資料と称して年が無い・現代の国境に年を付ける宣言の食い違いを防ぐ |
| `test_render_sha256_must_be_a_sha256_when_present` | 描画物との結びつきが任意の文字列にならない |
| `test_label_is_place_id_and_text` | ラベルの形（空文字なし） |
| `test_the_verdict_order_is_pass_warn_review_block` | 厳しさの順序の唯一の定義（`stricter`）が 4 値すべてで正しい |
| `test_a_verification_cannot_be_milder_than_its_worst_check` | BLOCK の check を持つ記録に PASS / REVIEW を付けられない。verdict は check から導く値（INV-94 の契約側） |
| `test_a_verification_needs_at_least_one_check` | 何も検査していない PASS を作れない |
| `test_disclaimer_markers_include_not_to_scale` | 模式図の表示語の定義が空にならない（INV-96 が空集合で自明に通るのを防ぐ） |

## `tests/unit/test_geo_verify.py`（決定論的検証。fake gazetteer: `tests/support/fake_gazetteer.py`）

fake gazetteer はメモリ上の固定データ（関ヶ原・大坂城・江戸/東京・京都・府中 2 件・コンスタンティノープル・
本州）。座標は実在地点のおおよその値で、距離・軸順・同名異所・旧称の意図に必要な精度だけを持つ。

| テスト | なぜ必要か / 落ちたら何が起きているか |
|---|---|
| `test_a_correct_map_passes` | 正しい地図が PASS（過剰な止め方をしていない）。全 check が PASS |
| `test_verification_is_deterministic` | 同じ入力から同じ結果（時計・乱数・集合の順序に依存しない） |
| `test_a_pin_hundreds_of_km_off_is_blocked` | T07。関ヶ原を東京の位置（約 280 km）に描く誤りを BLOCK する |
| `test_the_distance_tolerance_depends_on_the_feature_type` | 同じ約 33 km のずれが城なら BLOCK・地方なら PASS。一律閾値に退行したら落ちる |
| `test_lower_location_confidence_widens_the_tolerance` / `test_a_source_uncertainty_radius_widens_the_tolerance` | 信頼度・資料の不確かさで許容が変わる（設計書 §3.2 検証順 3） |
| `test_a_slightly_large_offset_is_a_warning_not_a_block` | 許容の 1〜1.5 倍は WARN（段階の途中が存在する） |
| `test_lat_lon_swap_is_blocked_by_bbox_and_reference` | T09。範囲内に収まる軸順の入れ替えを想定地域で検出 |
| `test_swap_is_detected_from_the_reference_even_when_the_bbox_is_wrong_too` | bbox が広すぎても参照位置との照合で拾う（片方の防衛線に頼らない） |
| `test_a_swap_that_leaves_the_valid_range_cannot_even_be_built` | 日本の座標の入れ替えは契約（INV-95）が先に弾く |
| `test_old_place_name_with_modern_coordinates_is_valid` | T08。旧称（江戸）が現代の座標と照合でき、旧称を誤検知しない |
| `test_a_modern_name_on_a_historical_map_needs_review` / `test_the_same_modern_name_is_fine_on_a_modern_context_map` | 時代に合わない現代の名を REVIEW にし、現代文脈の地図では止めない |
| `test_a_homonym_is_never_auto_picked` | T08。同名の参照が複数なら、片方に近くても自動で選ばない（REVIEW） |
| `test_an_explicit_catalog_id_resolves_a_homonym_and_the_distance_still_applies` | カタログ ID の明示は曖昧さを解くが、距離の検査は残る（取り違えた ID を素通りさせない） |
| `test_a_place_without_any_reference_cannot_pass` | 参照が無い地点が PASS にならない（fail-closed） |
| `test_modern_borders_on_a_historical_map_need_review` / `test_a_border_assertion_without_border_data_needs_review` / `test_historical_borders_must_match_the_map_time_basis` / `test_modern_borders_are_fine_on_a_modern_context_map` | T08。当時の資料の無い現代国境・資料の無い国境の主張・年の食い違いを REVIEW、現代文脈は止めない |
| `test_an_unparseable_time_basis_needs_review` / `test_time_basis_formats_are_accepted` | 時間基準が不明な歴史地図を通さない。受け付ける書式の固定 |
| `test_a_place_outside_its_valid_period_is_blocked_on_a_historical_map` / `test_the_references_own_period_also_applies` | 存在しなかった時代の地物は BLOCK、参照側の期間でも止まる |
| `test_a_label_for_an_unknown_place_is_blocked` / `test_a_misspelled_label_is_blocked` / `test_labels_match_by_normalized_display_name_or_a_known_alias` | T10。誤字・未知の地点を指すラベルを BLOCK、正規化と別名は誤検知しない |
| `test_a_claim_that_is_not_supported_needs_review` / `test_a_qualified_claim_resolves` / `test_a_claim_id_that_does_not_exist_needs_review` | 位置の主張が supported / qualified に解決しない地点を REVIEW（disputed / insufficient / 不在） |
| `test_a_schematic_without_the_disclaimer_is_blocked` / `test_a_schematic_with_the_disclaimer_passes` | T11・INV-96。縮尺不同の表示が無い・語が無い模式図を BLOCK、有れば PASS |
| `test_a_schematic_is_not_checked_against_real_locations` / `test_a_schematic_still_blocks_a_label_for_an_unknown_place` | 模式図は実在位置を主張しないので距離を見ない。ただしラベルの参照切れは止める |
| `test_a_scene_claiming_a_real_location_without_a_manifest_is_blocked` / `test_a_scene_without_a_real_location_claim_needs_no_manifest` / `test_a_schematic_cannot_back_a_real_location_claim` / `test_verify_scene_delegates_to_the_manifest_checks` | INV-93。manifest 無し・模式図での実在位置の主張を BLOCK。主張が無いシーンは要求しない |
| `test_a_non_schematic_map_with_no_places_cannot_pass` | 何も検証できない地図が PASS にならない |
| `test_the_verifier_does_not_mutate_its_inputs` | 純関数であること（注入した参照データを書き換えない） |

## `tests/unit/test_geo_combine.py`（決定論的ガードの優先。T32、INV-94）

| テスト | なぜ必要か / 落ちたら何が起きているか |
|---|---|
| `test_a_critic_pass_cannot_override_a_deterministic_block` | T32。QualityCritic が誤りを PASS と評価しても BLOCK のまま |
| `test_a_critic_pass_cannot_override_a_deterministic_review` | REVIEW（自動投稿に進めない）も critic の PASS / WARN で緩まない |
| `test_the_combined_verdict_is_never_milder_than_either_input` | 4×4 全組み合わせで結果 = 厳しい方。critic は等しいか厳しい側にしか動かせない |
| `test_a_stricter_critic_can_tighten_a_pass` | critic が厳しい側に動かすことは許す（助言として有効） |
| `test_the_deterministic_checks_are_kept_unchanged` | 結合が決定論的な check を消す・書き換えない（誰が止めたか追える） |
| `test_a_score_is_not_accepted_in_place_of_a_verdict` | 点数・文字列を verdict に写す入口が無い |
| `test_a_combined_record_cannot_be_hand_edited_to_pass` | 契約側の最後の防衛線（verdict を check から切り離して PASS にできない） |

## `tests/architecture/test_geo_boundaries.py`（境界）

| テスト | なぜ必要か / 落ちたら何が起きているか |
|---|---|
| `test_the_geo_domain_exists` | 検査対象が空で自明に通るのを防ぐ |
| `test_the_geo_domain_imports_no_io_clock_or_network_modules` / `test_the_geo_domain_does_not_call_io_builtins` | 検証器が時計・乱数・ファイル・ネットワークに触れると「同じ入力から同じ結果」が崩れ、テストが fake を使う意味が無くなる |
| `test_the_geo_domain_imports_only_contracts_and_itself` | INV-6（domain は contracts のみ）を geo について名指しで固定 |
| `test_production_billing_and_entrypoints_do_not_import_geo` | 未接続の保証。本番の課金・Episode 工程・API・workflow から geo が使われない（接続は後続の枝が ADR とともに決める） |
