# テスト設計の根拠: BGM の完成（ADR-0083 / INV-133〜138）

各テストが何の事故・退行を止めるために存在するか。落ちたら実装を直す（テストを緩めない）。
ADR-0077 の既存テスト（`docs/testing/bgm-render-rationale.md`）は変えていない。変えたのは次の 2 点だけで、いずれも ADR-0083 の
設計変更（Proposed）に伴う期待値の更新: (1) `tests/unit/test_bgm_ffmpeg_graph.py::EXPECTED_TAIL` の ducking の係数
（threshold=0.03 ratio=9 → 0.1 / 3。実音声で発話中の BGM が声より約 25 LU 低く聞こえない状態を実測したため）、
(2) 同ファイルの末尾に `source_offset_ms` のテストを追加。

## 配置と選曲（INV-134）`tests/unit/test_bgm_placement.py`

| テスト | 止めるもの |
|---|---|
| `test_scene_windows_follow_the_storyboard_scenes_each_voice_covers` | 台本 scene と storyboard scene の対応を取り違えた時間窓（cue が別の場面に掛かる） |
| `test_the_same_inputs_give_the_same_cue_sheet` | 配置に非決定性（辞書順・時刻・乱数）が入り、同じ Episode で render の input_hash が毎回変わって再描画される |
| `test_default_roles_open_with_intro_and_close_with_outro_and_the_sheet_is_resolvable` | 作った cue sheet が描画側の再検査（ライセンス・時間軸・scene）に落ちる = 配置と描画の契約の食い違い。動画の頭/末尾のフェードの欠落 |
| `test_one_continuous_run_keeps_the_same_music_position_across_scene_boundaries` | scene の境目ごとに曲が頭から鳴り直す（最も目立つ欠陥）。境目のクロスフェードの欠落 |
| `test_a_silent_scene_has_no_music_and_the_music_restarts_after_it` | 「この場面は無音」と指定したのに音楽が掛かる。無音の手前でぶつ切りになる |
| `test_roles_set_the_level_and_climax_is_louder_than_body_but_never_above_the_cap` / `test_a_very_quiet_track_is_clamped_...` | 役割で音量が変わらない、山場が body より小さい、曲の実測ラウドネスを補正しない、ナレーション優先の上限を超える |
| `test_placement_refuses_instead_of_guessing`（4 件） | ラウドネス未測定の曲・未承認の曲・全 silent・未知の scene で、推測した cue sheet を出す |
| `test_cues_carry_the_catalog_facts_verbatim_including_the_credit_text` | 音源 sha・証拠 hash・帰属文が cue に正しく写らず、描画時の再検査で BGM が黙って落ちる |
| `test_scenes_shorter_than_the_fades_still_give_a_valid_sheet` | 短い scene で fade の合計が cue を超え、契約違反で配置が例外になる |
| `test_choice_is_deterministic_and_prefers_mood_then_less_used_then_energy` | 入力の並びで選曲が変わる、energy が mood/使用回数より強く効く |
| `test_choice_never_returns_a_track_that_fails_a_license_fact` | 失効・取消の曲が唯一の候補でも選ばれる |

## カタログの保存と再利用（INV-133）`tests/unit/test_bgm_catalog_store.py`、`test_bgm_synth.py`

| テスト | 止めるもの |
|---|---|
| `test_registering_writes_...` | 登録した曲が content-addressed の場所に置かれない・カタログに載らない |
| `test_the_same_bytes_are_reused_not_registered_again` | 別名での再申告が新しい承認（`revoked` を `approved` に等）を生む、同じ内容が二重に載る |
| `test_a_lying_sha_or_a_swapped_track_id_...` | 申告と実バイトの不一致・同じ track_id の差し替え（変造）が登録される。拒否時に半端な書き込みが残る |
| `test_a_tampered_original_...` / `test_a_corrupt_catalog_...` | 内容の違う原本・壊れたカタログを黙って上書きして証拠を失う |
| `test_what_is_written_is_what_the_render_source_reads_back` | 書き手（登録・配置）と読み手（描画の供給元）の配置が食い違い、cue sheet が読めず BGM が黙って使われない |
| `test_bgm_synth.py`（4 件） | 検証用の合成曲が非決定的、無音/クリップ、ループの継ぎ目でクリックが出る（検証が曲の欠陥に汚染される）、長さの制約違反 |

## ナレーション優先（INV-135）`test_bgm_intelligibility.py`、`test_bgm_real_ffmpeg_margin.py`、`tests/architecture/test_bgm_phase3_boundaries.py`

| テスト | 止めるもの |
|---|---|
| `test_margin_is_voice_minus_music_...` / `test_one_scene_below_...` | 最小の差でなく平均で判定する、閾値ちょうどの扱いの取り違え、1 scene の聞き取りづらさが見逃される |
| `test_a_scene_without_music_is_not_a_failure_but_unmeasured_voice_is_not_a_pass` | 測れていないものを合格にする。BGM が無い区間を不合格にする |
| `test_the_music_stem_reuses_the_real_bgm_chain_...` | 計測用の graph が本番の混合とずれ、測った値が本番の音を表さない。未接続の出力で ffmpeg が失敗する |
| `test_the_voice_stem_is_the_narration_bus_of_the_bgm_less_render` | 声の側の計測が BGM 無しの音声と違う合算になる |
| `test_ducking_lowers_the_music_under_speech_and_the_probe_measures_it`（実 ffmpeg） | ducking が効いていない、計測器が声と BGM を取り違える。固定版 ffmpeg が無い環境では skip |
| `test_a_louder_cue_gives_a_smaller_margin`（実 ffmpeg） | 差の符号・単調性の誤り |
| `test_the_calibrated_constants_are_defined_only_in_the_contract` | 校正した閾値を別モジュールが書き直し、片方だけ更新される |

## BGM だけの変更（INV-137）

| テスト | 止めるもの |
|---|---|
| `tests/unit/test_bgm_only_change.py::test_changing_only_the_track_or_the_placement_reruns_render_and_nothing_else` | 曲・配置・役割の変更が fal 呼び出し・予約・production 成果物・入力 hash に波及する（高額な再生成）。本物の配置関数で cue sheet を作るので、配置側の追加が production を巻き込む退行も止める |
| `test_bgm_phase3_boundaries.py::test_phase3_bgm_modules_do_not_import_production_or_provider_code` / `..._render_worker_still_loads_no_fal_...` | 新しい部品が production / fal の module を import し、Render worker に読み込まれる |

## 品質検査（INV-138）`tests/unit/test_bgm_quality_checks.py`

| テスト | 止めるもの |
|---|---|
| `test_not_used_is_an_explicit_pass_bound_...` | 「BGM 不使用」が検査の欠落（無検査 = PASS）と区別できない、別の動画の PASS を流用できる |
| `test_a_clean_bgm_video_passes_...` | 正常系で偽の BLOCK、証跡への結び付きの欠落 |
| `test_credit_lines_must_equal_the_catalog_text` | クレジット原文の欠落・改変・重複が通る |
| `test_evidence_of_another_video_is_blocked_...` | 別の動画の証跡で通る |
| `test_a_track_no_longer_valid_or_changed_since_rendering_is_blocked`（7 件） | 描画後に失効・取消・隔離・Shorts 不可・音源差し替え・証拠更新された曲が通る |
| `test_a_track_missing_from_the_catalog_is_blocked` | カタログに無い曲が通る |
| `test_out_of_range_loudness_...`（4 件）/ `test_the_check_recomputes_...` / `test_the_local_loudness_rule_agrees_with_evaluate_loudness_everywhere` | 保存された `passed` を信用する（偽造）、domain/music に持った判定が `evaluate_loudness` とずれる |
| `test_speech_margin_unmeasured_is_review_and_too_small_is_block` | 未測定を PASS にする、閾値未満が通る |
| `test_details_are_fixed_ascii_...` / `test_naive_now_...` | ID・帰属文が結果に漏れる（INV-106）、naive な時刻での期限比較 |

## グラフ `test_bgm_ffmpeg_graph.py`

| テスト | 止めるもの |
|---|---|
| `test_source_offset_starts_the_track_mid_way_and_zero_keeps_the_chain_unchanged` | offset が filtergraph に反映されず曲が鳴り直す。offset 0 のとき従来の文字列から変わる（スナップショットの意図せぬ変更） |

## 実素材での検証（テストではなく手動の検証）

`tests/support/bgm_verify_export.py`: 本番 Episode の読み取り専用 export を本番の `RenderActivities`（固定版 ffmpeg・本物の probe）で
描き、BGM 無し/有り/配置変更の 3 本を作って測る。結果は `docs/architecture/v3-phase3-bgm-verification.md`。
CI では走らせない（実 export と固定版 ffmpeg が要る）。
