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
