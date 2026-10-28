# テスト設計の根拠: BGM の Render 結線（ADR-0077）

各テストが何の事故・退行を止めるために存在するか。落ちたら実装を直す（テストを緩めない）。

## 同一性（INV-113）

| テスト | レベル | 止めるもの |
|---|---|---|
| `tests/unit/test_bgm_off_golden.py`（3 件） | unit（純粋関数） | BGM 結線の副作用で、OFF のときの input_hash / filtergraph / argv が変わること。変わると本番の現行 final_video が「別入力」扱いになり全 Episode が再描画される。期待値は結線前のコミットで実測した固定値 |
| `tests/unit/test_bgm_resolve.py::test_no_cue_sheet_...` | unit | cue sheet が無いのに BGM のキーが hash に入ること |
| `tests/unit/test_bgm_resolve.py::test_a_usable_cue_sheet_changes_...` | unit | レベル・フェード・終了時刻・ducking のどれかを変えても hash が動かず、古い BGM の動画が再利用されること |
| `tests/unit/test_bgm_resolve.py::test_narration_priority_...` | unit | cue が 0 dB 等を指定してもナレーションより大きくなること。静かな cue まで上限へ引き上げてしまうこと |

## ライセンス（INV-114）

| テスト | レベル | 止めるもの |
|---|---|---|
| `test_a_track_that_fails_any_license_fact_...` | unit | 承認・状態・期限・再利用・商用・改変・形式のどれか 1 つの欠けで、唯一の曲が鳴ること |
| `test_a_track_expiring_after_...` | unit | cue sheet 作成後に失効した曲が、描画時の再検査で通ること（境界は期限ちょうどを含む） |
| `test_a_shorts_only_restriction_...` | unit | profile の形式と曲の許可形式の取り違え（Shorts 不可の曲が Shorts に、逆に長尺専用が除外されない） |
| `test_unknown_track_altered_or_missing_...` | unit | カタログに無い曲・差し替わった/無い実ファイル・古い証拠 hash の cue が使われること |
| `test_one_bad_cue_drops_all_of_bgm` | unit | 悪い cue だけ落として残りを鳴らす（演出が壊れ、落ちた理由が曖昧になる） |
| `test_cue_outside_the_timeline_...` / `test_an_unknown_render_profile_...` | unit | 動画の尺を超える cue・未知の scene・形式の分からない profile の BGM |
| `test_credit_lines_are_the_catalog_text_exactly` | unit | 帰属文が cue のコピーや再生成で改変されること |
| `test_naive_now_is_refused` | unit | naive な時刻での期限比較 |

fixture の曲・hash は架空で、実在の楽曲・ライセンス判断ではない。
