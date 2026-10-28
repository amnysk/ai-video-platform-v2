# テスト設計の根拠: description の内部 ID lint と marker（ADR-0070 / INV-90〜92）

落ちたら実装を直す（テストを緩めない）。flag は既定 OFF で、OFF の挙動は従来と byte 単位で同じ。

| テスト（`tests/contract/test_upload_metadata_lint.py`） | 止めるもの |
|---|---|
| `test_flag_is_off_by_default` | 未設定・`0` で ON になる事故（どの env でも ON にしない約束の機械側） |
| `test_flag_off_is_byte_identical_to_the_legacy_shape` / `test_insert_body_is_unchanged_with_flag_off` | 追加が既定の挙動（description 末尾の marker・送信 body）を変えること |
| `test_description_never_contains_internal_ids`（T17、parametrize） | marker・UUID・MinIO URL / key が本文・必須部分から視聴者に出ること。種類ごとに1件ずつ |
| `test_lint_leaves_ordinary_text_alone` / `test_flag_off_does_not_lint` | lint の過検出（通常文・URL・24桁 hex 単体）と、OFF で旧動画の形を壊すこと |
| `test_insert_body_description_has_no_marker` / `test_insert_body_lints_the_final_body` | builder を通らない metadata（旧 snapshot 等）が送信直前の検査をすり抜けること |
| `test_tags_keep_the_reconciliation_marker`（T18） | description の変更で照合キー（tags の marker）まで消えること。description の検査とは**別**に固定（設計 §5.2 5） |
| `test_lookup_matches_tags_only_and_legacy_description_only_videos`（T19） | 二重投稿防止の照合が tags のみ・旧 description のみのどちらかで効かなくなること（fake レベル。実 YouTube は未検証） |
| `test_required_parts_over_the_limit_raise_instead_of_truncating`（T29） | 必須の帰属が黙って欠けること |
| `test_required_parts_survive_a_long_body` / `..._exactly_filling_the_budget_are_kept` | 本文が長いときに必須部分が先に削られること、境界（ちょうど 5000 bytes）の取り違え |

既存の `tests/unit/test_upload_hardening.py::test_metadata_description_ends_with_the_marker_even_when_truncated`
は既定（OFF）の挙動の検査として無変更で有効。T20 / T21 の既存テストも無変更。
