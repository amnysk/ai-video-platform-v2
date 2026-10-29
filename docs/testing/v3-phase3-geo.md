# v3 Phase 3 geo（ADR-0084）: テストを置いた理由

各テストが**何の失敗を防ぐか**を残す（AGENTS.md / メモリの方針）。ADR-0084・INV-139〜145。

## tests/unit/test_geo_p3_gate.py（manifest の組み立てと正確さの関門）

| テスト | 防ぐ失敗 |
|---|---|
| `test_a_well_formed_request_passes_and_takes_coordinates_from_the_gazetteer` | 依頼から作った manifest の座標が gazetteer と違う（作者が座標を混ぜる）。PASS の基準線 |
| `test_the_same_request_always_builds_the_same_manifest` | 地点の並び順で manifest（= hash）が変わり、同じ依頼が別の入力になる |
| `test_a_request_cannot_carry_coordinates` | 依頼に座標を書く経路が生えて、位置違いが検証の外から入る（INV-139） |
| `test_an_unknown_gazetteer_reference_is_blocked_not_guessed` | gazetteer に無い地点を名前から推測して置く |
| `test_a_duplicated_reference_in_a_request_is_ambiguous` / `test_a_duplicated_gazetteer_entry_is_ambiguous` | 同じ ref_id が複数あるのを自動で 1 つに決める |
| `test_a_place_without_a_claim_or_without_a_source_is_blocked` | 根拠の無い地点が描かれる（負例「出典なし」の理由コード固定） |
| `test_a_source_that_is_not_in_the_research_evidence_is_refused` | 出典 id が research の資料に解決しないのに通る（`sources` を渡し忘れても気づく） |
| `test_a_source_that_was_not_fetched_is_not_a_citation` | 取得失敗の資料が「出典」として数えられる |
| `test_an_insufficient_claim_is_not_evidence` / `test_an_unresolved_claim_id_is_refused` | 評価が足りない・存在しない claim が根拠になる |
| `test_a_modern_name_on_a_twelfth_century_map_is_an_anachronism` | 1185 年の地図に "Tokyo" が載る（負例「時代錯誤」） |
| `test_a_name_that_belongs_to_another_place_is_blocked` | 別の地点の名前を貼る |
| `test_an_unknown_time_basis_on_a_historical_map_is_refused` | 時間基準が読めない地図が通る（REVIEW でも使わない） |
| `test_a_modern_border_on_a_historical_map_is_refused` | 現代の国境を当時の地図に使う |
| `test_a_manifest_with_wrong_coordinates_is_blocked_by_the_verifier` | LLM が書いた manifest の位置違い（負例「座標違い」）。依頼を経ない経路の防御 |
| `test_swapped_axes_are_blocked` | 緯度経度の取り違え |
| `test_a_schematic_map_without_the_disclaimer_is_blocked` | 模式図の免責無し（INV-96）が結線後も効く |
| `test_input_fingerprints_ignore_order_and_change_with_content` | gazetteer・根拠の変更が判定の再利用を許す（指紋が内容に追従しない） |

## tests/unit/test_geo_p3_record_and_quality.py（証跡の鎖と品質検査）

| テスト | 防ぐ失敗 |
|---|---|
| `test_the_digest_format_matches_the_renderers_manifest_hash` | 証跡と描画器で manifest の指紋の式が食い違い、鎖が黙って切れる（INV-6 で domain が hashing を import できないための二重定義の番人） |
| `test_a_passing_request_yields_a_bound_pass_record_and_an_image` | PASS の記録に画像・manifest の hash が無い |
| `test_preparing_twice_gives_identical_records_and_bytes` | 同じ入力で記録・PNG が揺れる（決定論） |
| `test_a_refused_map_is_never_drawn` | 誤った位置の画像が作られ、後で使われうる |
| `test_a_map_whose_text_cannot_be_drawn_is_not_applied` | CJK が豆腐のまま使われる（INV-145）。描いた事実は証跡に残る |
| `test_the_record_refuses_to_pass_without_the_presentation` / `test_the_record_refuses_a_verdict_milder_than_its_findings` | 契約が「PASS を手で書く」入口になる |
| `test_tampering_with_the_manifest_or_the_verification_breaks_the_binding` | 証跡の改ざん・不整合を再計算で見抜けない |
| `test_a_pass_record_needs_a_clip_and_the_same_final_video` | クリップ無し・別の完成動画の証跡が PASS 扱い |
| `test_an_episode_without_a_map_gets_an_explicit_hash_bound_pass` | 「地図なし」が「検査していない」と区別できない / 別の動画の「なし」を流用 |
| `test_a_verified_and_bound_map_passes_with_the_record_as_evidence` | 正常系の evidence_ref が記録に結びつかない |
| `test_refused_maps_are_not_passes` | 位置・出典・時間基準の拒否が PASS になる。理由コード（GEO_MISMATCH / GEO_TIME_BASIS_UNKNOWN）の固定 |
| `test_a_presentation_only_defect_is_a_visual_defect` | 見た目だけの欠陥が正確さの理由に混ざる |
| `test_a_specified_map_with_no_evidence_is_blocked_not_treated_as_not_used` | 証跡の欠落が「地図なし」に化ける（fail-open） |
| `test_evidence_for_another_final_video_is_blocked` | 古い・別の動画の証跡の流用 |
| `test_two_records_for_one_scene_are_ambiguous` | 同じシーンの証跡が複数ある曖昧さ |
| `test_the_checks_never_echo_input_strings_and_are_deterministic` | 入力の文字列が detail に漏れる（INV-106）。同じ入力で結果が揺れる |
| `test_a_malformed_final_video_hash_is_a_programming_error` | 不正な hash を黙って受けて evidence_ref が意味を失う |

## tests/unit/test_geo_p3_infra.py（供給元・下地・クリップ）

| テスト | 防ぐ失敗 |
|---|---|
| `test_a_directory_source_loads_specs_gazetteer_and_evidence` | 供給元の形式（オーナーに渡す契約）が読めない |
| `test_an_episode_without_specs_has_no_map` | 指定の無い Episode が失敗する（OFF 同然であるべき） |
| `test_a_present_but_unreadable_file_is_an_error_not_no_map` | 壊れた指定が「地図なし」になり検査が外れる（fail-open） |
| `test_a_gazetteer_without_a_license_or_curator_is_refused` | 出所・ライセンスの分からない参照データを使う |
| `test_a_missing_gazetteer_is_an_error` | gazetteer 無しで検証が成立したことになる |
| `test_an_unsafe_episode_id_never_reaches_the_filesystem` | episode id によるパス脱出 |
| `test_basemap_keeps_only_rings_that_touch_the_region` / `test_a_broken_basemap_is_an_error` | 無関係な外周で描画が重い・壊れた下地が黙って無視される |
| `test_a_basemap_changes_the_image_and_its_identity_only_when_given` | 下地なしの出力・identity が従来と変わる（INV-119 の退行）。下地の注記が描かれない |
| `test_the_base_caption_is_part_of_the_layout` | 注記が見た目の検査の対象外になる |
| `test_the_render_spec_follows_the_profile_resolution` | profile と違う解像度で描いて拡縮される |
| `test_the_service_identity_changes_with_the_verdict_and_the_inputs` | gazetteer・クリップ長の変更が render の再利用に反映されない / 非決定 |
| `test_the_service_refuses_an_out_of_range_scene_duration` | 異常な尺でクリップを作る |
| `test_the_real_encoder_makes_a_profile_sized_mp4_of_the_requested_length` | 固定版 ffmpeg で 1080x1920・指定尺・bitexact の mp4 にならない（実機。無い環境は skip） |
| `test_a_failing_encoder_is_a_retryable_render_error` | ffmpeg の失敗が握りつぶされる / 再試行できない型になる |
| `test_japanese_labels_pass_with_the_pinned_cjk_font_and_the_font_is_in_the_identity` | CJK が実フォントで描けない（Phase 2 §4-6）。既定フォントでは使われない（無い環境は skip） |

## tests/unit/test_render_activities_geo.py（Render Activity の結線）

| テスト | 防ぐ失敗 |
|---|---|
| `test_flag_off_writes_no_geo_evidence_and_keeps_the_same_identity` | OFF で証跡が増える・動画の入力が変わる（INV-143） |
| `test_flag_on_without_specs_writes_an_explicit_not_used_evidence` | ON の Episode に証跡が無く、ゲートが「検査していない」と「地図なし」を区別できない |
| `test_a_verified_map_replaces_that_scenes_video_and_is_bound_to_the_final_video` | 地図が差し替わらない / 他のシーンまで変わる / クリップ長が実尺と違う / 証跡が完成動画に結びつかない |
| `test_a_refused_map_keeps_the_original_video_and_the_evidence_blocks_the_upload` | 拒否された地図が使われる。ゲートに渡る BLOCK と、入力の文字列が漏れないこと |
| `test_applying_or_refusing_a_map_changes_the_render_identity` | 判定が変わっても古い動画を再利用する |
| `test_the_same_inputs_are_not_rendered_twice` | 同じ入力で毎回描き直す（INV-17） |
| `test_a_spec_that_points_at_the_wrong_scene_stops_the_render` | 別の台本シーンの地図を貼る |
| `test_an_unreadable_source_stops_the_render_instead_of_meaning_no_map` | 供給元の故障で証跡の無い動画ができる |
| `test_no_final_video_row_exists_without_its_geo_evidence` | 証跡の無い完成動画の行が存在する |

## tests/unit/test_production_plan_geo.py（Production の事前検査）

| テスト | 防ぐ失敗 |
|---|---|
| `test_flag_off_does_not_look_at_geo_at_all` / `test_no_specs_means_the_plan_is_unchanged` / `test_a_good_map_leaves_the_plan_unchanged` | 事前検査が計画（= workflow の分岐）を変える。OFF で動く |
| `test_a_map_that_would_be_refused_stops_production_before_any_paid_call`（4 通り） | 時代錯誤・出典不解決・根拠なし・時間基準不明の地図で fal を呼んでしまう。メッセージに入力の写しが出ない |
| `test_a_spec_for_a_scene_outside_its_script_scene_is_an_input_error` | 台本シーンと storyboard シーンの対応違い |
| `test_an_unreadable_source_is_not_treated_as_no_map` | 事前検査が壊れた指定を見逃す |

## tests/unit/test_geo_p3_flag.py（flag・identity）

| テスト | 防ぐ失敗 |
|---|---|
| `test_geo_none_keeps_the_pre_geo_input_hash` | `geo=None` で既存の render input hash が動く（実測の golden に固定） |
| `test_any_geo_identity_changes_the_hash_even_when_it_lists_no_map` | ON の印が hash に入らず、証跡の無い従来の動画が再利用される |
| `test_the_geo_identity_and_the_bgm_identity_do_not_collide` | geo と bgm の payload キーが衝突 |
| `test_maps_cannot_be_enabled_while_the_quality_gate_is_not_enforced` / `test_maps_may_be_enabled_with_the_gate_enforced` | ゲートが効かないまま地図を有効にして、拒否された地図が投稿される |
| `test_the_default_is_off` | 既定が ON になる |

## tests/architecture（境界）

`test_geo_boundaries.py` の追加: 検出器そのものの検査 3 件（`from pkg import submodule`・相対 import・動的 import。Phase 2
報告 §4-8 の抜け。**修正前の検出器は 3 つとも見逃す**）/ 結線が provider・network を import しない / 子プロセスは
`clip.py` だけ / workflow と Activity 契約が結線を import しない（履歴の互換）/ flag の読み手が worker の組み立てだけ。
`test_geo_maps_boundaries.py`: 検出器を `tests/support/import_scan.py` へ移し共有（定義を 1 箇所に。挙動は同じ。既存の検出器
fixture テストはそのまま通る）。`GEO_CONSUMER_EXEMPT` に `infrastructure/geo` を足した（worker は結線の窓口だけを import し、
geo の契約・ドメインを直接 import しない検査は維持）。
