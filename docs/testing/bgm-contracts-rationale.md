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
