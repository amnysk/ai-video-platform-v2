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

## ffmpeg の混合（INV-113 / ADR-0077 §4）

| テスト | レベル | 止めるもの |
|---|---|---|
| `tests/unit/test_bgm_ffmpeg_graph.py::test_bgm_graph_tail_snapshot_...` | unit（文字列） | ducking/fade/limiter の組み方が黙って変わること（変えるなら `BGM_MIX_VERSION` を上げ、この snapshot を更新する）。映像側の filtergraph が BGM 有効時に変わること |
| `test_duck_cue_uses_the_narration_bus_...` / `test_zero_length_fades_...` | unit | ducking 無しの cue にサイドチェーンが付く・長さ 0 のフェードで ffmpeg が失敗する filter が出ること |
| `test_argv_adds_one_looping_input_per_cue_...` / `test_argv_refuses_misaligned_...` | unit | 入力番号と filtergraph のずれ（別の曲が混ざる）。BGM 無しで BGM 入力を渡すこと |
| `tests/unit/test_render_ffmpeg_real.py::test_bgm_renders_decodes_and_ducks_under_the_voice` | unit（実 ffmpeg。同ファイルの既存条件付き skip に従う） | filtergraph が実 ffmpeg で通らない・曲が混ざっていない・ducking が効かない・短い曲が繰り返されず cue が欠けること。測定は ffmpeg の volumedetect（numpy 不要） |
