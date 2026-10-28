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

## ラウドネス（INV-115）

| テスト | レベル | 止めるもの |
|---|---|---|
| `tests/unit/test_bgm_loudness.py::test_real_ffmpeg_summary_is_parsed_...` / `..._silent_summary_...` | unit（固定 fixture） | ffmpeg 出力形式の取り違え（小数の丸め・`-inf`・無音の -70.0）。fixture は固定版 ffmpeg 7.1.1 の実出力 |
| `test_unreadable_output_is_an_error_never_a_pass` | unit | 要約が無い・欠けているときに既定値で合格すること |
| `test_verdict_reason_codes_and_boundaries` | unit | 境界値（ちょうど下限/上限/天井）の取り違えと、複数違反時の理由の揺れ |
| `test_a_failed_verdict_becomes_a_failed_qa_check_that_raises` | unit | 不合格が QA の失敗クラスへ写像されず保存されること |
| `test_probe_that_cannot_run_raises_instead_of_passing` | unit | 実測バイナリが無い/失敗したときに「測れなかった=合格」となること |
| `tests/unit/test_render_ffmpeg_real.py::test_loudness_probe_measures_a_real_file` | unit（実 ffmpeg、既存の条件付き skip に従う） | 実 ffmpeg の引数・出力形式が parse と食い違うこと |

## Activity 結線・fal ゼロ（INV-113〜116）

| テスト | レベル | 止めるもの |
|---|---|---|
| `tests/unit/test_render_activities_bgm.py::test_no_cue_sheet_renders_exactly_like_no_bgm_support` | unit（SQLite+メモリ） | 設定 ON でも cue sheet が無い Episode の input_hash が変わり、現行 final_video が再利用されず全件再描画されること |
| `test_bgm_changes_identity_and_stores_evidence_...` | unit | BGM 入りが BGM 無しと同じ hash になり古い動画を再利用する・証跡（クレジット原文・曲 sha・証拠 hash・実測）が欠ける/final_video と食い違う・QA 報告に実測が残らない |
| `test_expired_or_unknown_license_renders_without_bgm_...` | unit | 失効・未確認・期限切れの曲が Render に渡る。黙って BGM を落とし理由がログに無い |
| `test_license_rejected_render_has_the_same_identity_as_no_bgm` | unit | ライセンス不適合の描画が BGM 無しと別 hash になり、不要な再描画を起こす |
| `test_a_track_that_expires_after_a_bgm_render_...` | unit | 描画後に失効した曲を含む現行動画が、次の描画でそのまま再利用されること |
| `test_unreadable_bgm_source_renders_without_bgm` | unit | 供給元の破損で描画が止まる/破損した入力で BGM を鳴らすこと |
| `test_out_of_range_loudness_is_a_structured_failure_...`（4 値） | unit | 大きすぎ・小さすぎ・天井超過・無音の動画が保存される。失敗後に証跡だけ残る |
| `test_bgm_only_changes_rerender_only_and_never_touch_production_or_fal` | unit | BGM の変更が fal 呼び出し・予約・production Artifact・render 以外の job を伴うこと（fal クライアントの全メソッドを失敗化して 0 件を確認）。変更なし再実行が描画すること |
| `tests/architecture/test_bgm_render_boundaries.py`（5 件） | architecture | Render worker が fal/production のコードを読み込む・BGM 層が production/provider を import する・設定キーを読む場所が増える・Activity 名/入出力フィールドが変わる（Temporal の履歴互換） |
| `tests/unit/test_bgm_setting.py` | unit | 既定が ON になる・キー名が複数箇所に散る |

## 暫定の供給元（`tests/unit/test_bgm_local_source.py`）

| テスト | 止めるもの |
|---|---|
| `test_no_cue_sheet_file_means_no_bgm_...` | cue sheet が無い Episode でカタログ読み込みの失敗が描画を止めること |
| `test_loads_cue_sheet_catalog_and_the_actual_file_sha` / `test_an_altered_file_...` / `test_a_missing_audio_file_...` | 差し替えられた・消えた音源をカタログの sha のまま「検証済み」と報告すること（実ファイルから sha を求める） |
| `test_broken_json_and_bad_episode_id_...` | 壊れた JSON・不正な episode_id（パス注入）が例外にならず通ること |
