# テスト設計の根拠: BGM 契約と選曲の純粋ドメイン（ADR-0072）

各テストが何の事故・退行を止めるために存在するか。落ちたら実装を直す（テストを緩めない）。
ここのテストは**契約と純粋関数だけ**を検査する。Render・説明欄・DB への結線は後続で、未検査（invariants INV-97〜100 の各欄参照）。

| テスト | 止めるもの |
|---|---|
| `tests/unit/test_music_contracts.py`（cue の不正） | end<=start・フェードが cue に収まらない・sha256 形式違反・scene_id 違反の cue が Render に渡ること |
| `tests/unit/test_music_contracts.py::test_level_is_integer_so_hash_is_stable` | `target_relative_level_db` に float / 文字列が黙って入り、cue sheet の正準 JSON hash が揺れること |
| `tests/unit/test_music_contracts.py::test_attribution_text_is_required_iff_attribution_required` | 帰属必須なのに指定文が空の曲が存在できること（T14 の前提）。帰属不要なのに文があり、無用なクレジットが出ること |
| `tests/unit/test_music_contracts.py::test_approved_requires_evidence_and_check_time` | 根拠 hash も確認日時も無い `approved`（人の証拠確認なしの承認）が表現できること |
| `tests/unit/test_music_contracts.py::test_invalid_track_is_rejected` | 不正 sha・未知の status・未知の format・naive datetime（期限比較が曖昧になる）・絶対パス/`..` の object_key |
| `tests/unit/test_music_contracts.py::test_usage_record` | Episode ごとの使用証跡が episode_id 形式不正で保存されること |

fixture（`tests/support/bgm_catalog.py`）の値は架空であり、実在曲・実在ライセンスの判断ではない。

## 選曲（`tests/unit/test_music_selection.py`、INV-97 / T13）

| テスト | 止めるもの |
|---|---|
| `test_only_legal_tracks_are_candidates_for_shorts` | 未確認・失効・Shorts 不可の曲が Shorts の候補に残ること。除外理由が機械可読で返ること |
| `test_each_license_fact_excludes_the_only_track` | 承認・状態・再利用・商用・改変・プラットフォーム・形式のどれか 1 つの欠けで、**唯一の曲でも**選ばれてしまうこと（「他に無いから」で妥協しない） |
| `test_valid_until_boundary_is_exclusive_of_now` | 期限ちょうどの曲の採用（境界の取り違え） |
| `test_naive_now_is_refused` / `test_now_is_not_read_from_the_clock` | 時計を domain が読むこと・naive 時刻での期限比較 |
| `test_attribution_required_without_text_is_excluded_even_if_contract_is_bypassed` | 契約を迂回した値（`model_construct`）が帰属文なしで選ばれること |
| `test_recent_use_is_soft_preference_only` / `..._never_excludes_the_only_legal_track` / `..._never_promotes_an_illegal_track` | 連投抑制が、合法な唯一の曲を除外する・違法な曲を押し上げる方向に働くこと |
| `test_mood_is_a_soft_preference_and_does_not_exclude` | mood の不一致で合法な曲が候補から消え、収集依頼が不要に積まれること |
| `test_ordering_is_deterministic_regardless_of_input_order` | 入力順で選曲が変わること（再実行で別の曲になる） |
| `test_empty_catalog_gives_no_candidates_so_caller_can_request_acquisition` | 候補なしを例外にせず、呼び出し側が収集依頼を積めること |

注: `modification_allowed=false` の曲も除外した。cue sheet はフェード・ducking（=改変）を前提とするため、設計 v3 §4.2 の項目を保守側に倒した（ADR-0072 に記録）。

## クレジット（`tests/unit/test_music_credits.py`、INV-98 / T14）

| テスト | 止めるもの |
|---|---|
| `test_credit_required_track_yields_exact_text` / `..._credited_once_in_first_use_order` | 指定文の改変・重複記載・順序の揺れ |
| `test_cue_for_a_track_missing_from_the_catalog_is_an_error` / `test_source_sha_must_match_the_catalog_file` | カタログに無い曲・別ファイルの cue から帰属表記を推測で作ること |
| `test_stale_or_altered_credit_copy_in_cue_is_an_error_not_silently_replaced` / `..._missing_credit_copy_...` | cue 側のコピーが古い・欠けているのに黙ってカタログ原文で上書きし、食い違いが見えなくなること |
| `test_fit_credits_blocks_instead_of_truncating` | 説明欄に入らないクレジットの切り捨て（BLOCK にせず短縮版を返す退行） |
| `test_fit_credits_counts_utf8_bytes_not_characters` / `..._counts_the_newline_separators` | 5000 UTF-8 bytes 制限を文字数や区切り抜きで数え、日本語の指定文で溢れること |
| `test_empty_credits_always_fit_even_with_zero_budget` / `test_negative_budget_blocks` | 帰属不要の曲だけの動画が BLOCK になること・負の予算の素通り |

## 取込検査（`tests/unit/test_music_ingest.py`、INV-99 / T12 / T27）

| テスト | 止めるもの |
|---|---|
| `test_sha_mismatch_is_rejected_as_tampered` / `test_malformed_actual_sha_is_rejected` | 申告と実ファイルの sha256 が違う（変造・取り違え）取込 |
| `test_same_file_is_deduplicated_to_the_existing_track` | 同じ音源の再収集・二重登録（T12） |
| `test_duplicate_never_upgrades_the_existing_license_facts` | 再取込の申告で既存の未確認曲が承認済みになること |
| `test_same_track_id_with_different_bytes_is_rejected` | 同じ track_id の中身の差し替え（immutable 違反） |
| `test_object_key_must_be_content_addressed_by_the_sha` | sha と無関係な object_key（上書き可能な配置） |
| `test_ingest_check_does_not_mutate_the_catalog` | 判定関数が副作用を持つこと |
