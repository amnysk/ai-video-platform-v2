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
