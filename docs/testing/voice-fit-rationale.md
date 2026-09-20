# テスト設計の根拠: 音声の実尺を描画の前に区間へ合わせる（ADR-0028）

各テストが**なぜテストになったか**を残す。事故は 2026-09-19 の Episode `87bbf7de`
（実尺 10,147 ms の音声が 7,000 ms の区間を超え、描画で `VoiceTimelineOverflowError`、
5 シーン中 5 つ）。根本原因は「実尺が分かる時点（音声の合成直後）で、誰も描画と同じ区間と
突き合わせていなかった」こと。ADR-0026 の予算は推定だった。

## `tests/unit/test_voice_fit.py`（純粋関数、domain）

区間との適合と話速の決め方は I/O を持たない規則なので、最下層（unit）で全分岐を固定する。
2026-09-20 の追補（実データ: 尺は話速に線形でない）で、探索を「線形の見積もり」から「実測を積む探索」
（`next_speed_permille(takes=...)`）に改めた。テストは尺を `固定 + 可変/倍率` にゆらぎを乗せたモデルで作る。

| テスト | なぜ必要か / 落ちたら何が起きているか |
|---|---|
| `test_no_adjustment_when_voice_already_fits` | 収まる音声を触らない。ja の既存台本・短い英語を新しい経路へ巻き込まない |
| `test_first_estimate_targets_a_little_inside_the_span` | 区間ぴったりを狙うと丸め・ゆらぎで再び溢れる。余裕を残す規則が消えたことを検出 |
| `test_estimate_beyond_the_cap_goes_straight_to_the_cap_not_to_failure` | **追補の核**。線形の見積もりが上限を超えても実測前に諦めない（固定の間があるので外挿は楽観的にも外れる）。失敗は上限で実測してから |
| `test_two_measurements_give_a_secant_estimate_for_a_fixed_plus_variable_model` | 2 点から固定の間を推定する。線形のままだと 9/19 s2 型で足りない |
| `test_prefers_the_slowest_speed_that_fits` | 上限へ飛ばない。収まる速さが上限より遅いなら、そこで止まる（聞こえ方を必要以上に変えない） |
| `test_real_data_case_fits_at_the_cap_where_a_linear_search_gave_up` | 9/19 s2 の実測型（区間 9,000 ms、等速 10,658 ms、上限で 8,975 ms）。旧方式は 9,067 ms で失敗した |
| `test_the_last_permitted_resynthesis_is_always_at_the_cap` | 途中の外挿が上限より遅くても、失敗を宣言する前に必ず上限で実測する（これを外すと上限で収まる入力を取りこぼす） |
| `test_failure_means_the_cap_speed_measured_too_long` | 失敗の根拠は「上限で実測して超えた」だけ。メッセージに全実測の数値を含める |
| `test_non_monotonic_measurements_fall_back_to_the_cap` | ゆらぎ・縮まない生成器で 2 点が右下がりでない。壊れた外挿（ゼロ除算・負の傾き）をせず上限で確かめる |
| `test_cap_is_a_parameter_so_profiles_can_differ` | 上限を 1 つの定数に縛らない（Shorts 固有値のハードコード禁止） |
| `test_search_invariants_over_fixed_pause_ratio_and_jitter` | 超過率 7 × 固定の間 5 × ゆらぎ 4 = 140 通りの性質検査: 合成は最大 4 回・話速は単調増加で上限以下・成功なら収まる・失敗なら上限で実測して溢れた・上限で（ゆらぎ込みで）収まる入力は必ず成功。個別の数値例が見落とす境界を機械的に探す |
| `test_check_voices_fit_spans_accepts_exact_fit_and_shorter` | 区間ちょうどは収まる（描画の判定は `終わり > 次の開始` だけを拒む）。境界を 1ms ずらす退行を検出 |
| `test_check_voices_fit_spans_reports_every_offender` | 9/19 は 5/5 が溢れた。最初の 1 件だけ直して再実行を繰り返す運用にしない |
| `test_check_voices_fit_spans_ignores_scenes_without_a_voice_yet` | 欠けは manifest の coverage 検査の責務。責務を混ぜない |
| `test_exceeds_span_is_needs_input_and_a_timeline_overflow` | 再実行で直らない欠陥を retryable にして課金付きの無限リトライにしない（failure-policy）。型名レジストリにも載ること（INV-12） |

## `tests/unit/test_production_voice_fit.py`（音声 Activity、SQLite + Fake）

実尺は合成しないと分からないので、Activity を Temporal 抜きで直接呼び、Fake 生成器の尺を文字数で決める。

| テスト | なぜ必要か |
|---|---|
| `test_voice_longer_than_span_is_resynthesized_faster_to_fit` | **Test A の核**。実尺 > 区間でも、描画の前に区間へ収まった音声が Artifact になる。話速が来歴に残り、等速の音声と区別できる |
| `test_voice_that_fits_is_synthesized_once_at_neutral_speed` | 収まる音声は 1 回合成・等速・profile 不変（INV-17 の input_hash と来歴が揺れない） |
| `test_voice_beyond_the_speed_bound_fails_as_needs_input_before_render` | 上限超は needs_input・non_retryable・job に needs_input を記録・溢れる音声を現行にしない |
| `test_generator_without_speed_control_fails_instead_of_overflowing_later` | 調整できない生成器は即失敗（黙って通して描画で落とさない）。無駄に再合成しない |
| `test_resynthesis_is_bounded_when_speed_does_not_shorten_the_voice` | 尺が縮まない壊れた生成器で無限に合成し直さない（上限つき） |
| `test_real_data_case_fits_at_the_cap_speed_where_a_linear_search_gave_up` | 追補の実データ（9/19 s2）を Activity 経由で: 固定の間・ゆらぎのある Fake（`MeasuredModelFake`）で、旧方式が失敗した入力が上限の話速で収まり、話速が来歴に残る |
| `test_a_mild_overrun_is_fitted_at_a_speed_below_the_cap` | 軽い超過は 2 回の合成・上限未満の話速で済む（上限へ飛ばさない） |
| `test_failure_is_declared_only_after_the_cap_speed_was_measured` | 上限でも収まらない入力: needs_input・non_retryable・job に needs_input・合成は最大 4 回・最後は上限の話速・メッセージにシーンと上限 |
| `test_last_scene_is_held_to_the_storyboard_end_too` | 最後の台本シーンの区間（storyboard の終端まで）も同じ規則。区間の計算が最後だけ別になる退行を検出 |

## `tests/unit/test_piper_voice_adapter.py` の話速指定（Piper adapter。architecture テストが adapter を import してよい module を限っているので既存ファイルに置く）

| テスト | なぜ必要か |
|---|---|
| `test_piper_can_synthesize_at_a_given_speed` | Activity は `isinstance(…, SpeedAdjustableVoiceGenerator)` で調整可否を決める。Piper が外れると本番では黙って調整が効かなくなる |
| `test_speed_is_applied_relative_to_the_voice_default_length_scale` | 話速 → `length_scale` の変換（既定値を倍率で割る）。向きを間違えると遅くなって悪化する。声質（noise）を変えない |
| `test_speed_adjustment_does_not_change_the_base_profile` | 呼び出しごとに基の profile が変わると input_hash（INV-17）が揺れる |
| `test_speed_below_neutral_is_refused` | 遅くする用途は無い。上限つきの「速くする」以外を受けない |

## `tests/unit/test_production_voice_gate.py`（ProductionWorkflow、time-skipping Temporal）

| テスト | なぜ必要か |
|---|---|
| `test_voice_that_cannot_fit_stops_production_before_any_paid_media` | 9/19 は課金後に描画で落ちた。音声が収まらないとき画像・動画（有料）を 1 件も起動しない。ここが破れると「安く早く止める」目的が失われる |
| `test_media_starts_only_after_every_voice_succeeded` | 順序の契約（音声 → 画像・動画）。正常系が並行に戻る退行を検出 |

補足: 既存 `tests/integration/test_production_workflow.py::test_terminal_failure_cancels_in_flight_awaits` は
「音声の失敗で進行中の画像 await が cancel される」を検査していたが、音声が先に済む設計（ADR-0028）では
成立しない。検査の意図（兄弟の終端的な失敗で進行中の await が cancel される）は変えず、失敗を
別シーンの画像 submit に移した（コミットメッセージに理由）。

## `tests/unit/test_production_assemble_voice_fit.py`（マニフェスト組み立て、SQLite）

| テスト | なぜ必要か |
|---|---|
| `test_overflowing_voice_stops_assembly_before_render` | 検査を持たない版の worker が作った音声・storyboard だけ差し替わった世代の音声が現行に残りうる。描画の直前の入口で同じ検査をかける最終の契約検査 |
| `test_voices_that_fit_pass_the_check` | 区間ちょうどで誤検出しない（この後は既存の欠け検査に進む） |
| `test_layout_matches_the_spans_asserted_above` | 上 2 つが前提にする区間（8,000 / 9,000 ms）が fixture と食い違ったら、テスト自体が無意味になる |

## ja-JP・非 Shorts への影響

区間は storyboard の並びだけで決まり、この変更は尺の定数を持たない。ja の既存音声テスト
（`tests/unit/test_production_voice_activities.py`）は無変更で通る（収まる音声は 1 回合成・等速）。
