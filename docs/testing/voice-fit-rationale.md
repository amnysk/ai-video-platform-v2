# テスト設計の根拠: 音声の実尺を描画の前に区間へ合わせる（ADR-0028）

各テストが**なぜテストになったか**を残す。事故は 2026-09-19 の Episode `87bbf7de`
（実尺 10,147 ms の音声が 7,000 ms の区間を超え、描画で `VoiceTimelineOverflowError`、
5 シーン中 5 つ）。根本原因は「実尺が分かる時点（音声の合成直後）で、誰も描画と同じ区間と
突き合わせていなかった」こと。ADR-0026 の予算は推定だった。

## `tests/unit/test_voice_fit.py`（純粋関数、domain）

区間との適合と話速の決め方は I/O を持たない規則なので、最下層（unit）で全分岐を固定する。

| テスト | なぜ必要か / 落ちたら何が起きているか |
|---|---|
| `test_no_adjustment_when_voice_already_fits` | 収まる音声を触らない。ja の既存台本・短い英語を新しい経路へ巻き込まない |
| `test_speedup_targets_a_little_inside_the_span` | 区間ぴったりを狙うと丸め・句読点の間で再び溢れる。余裕を残す規則が消えたことを検出 |
| `test_speedup_is_relative_to_the_speed_already_applied` | 再合成の 2 回目は、1 回目の話速を土台にする（絶対値で計算すると効かない） |
| `test_speedup_beyond_the_bound_is_refused_not_clamped` | 9/19 の実測（10,147 / 7,000 ms）は上限内で収まらない。丸めて通すと聞き取れない音声になる。失敗として返す |
| `test_bound_is_a_parameter_so_profiles_can_differ` | 上限を 1 つの定数に縛らない（locale・profile が違っても同じ関数で使える。Shorts 固有値のハードコード禁止） |
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
