# Storyboard の映像対象（ADR-0035 (1)）のテスト設計根拠

事故: 2026-09-26/27、写実的で顔が識別できる人物を主題にしたシーンの入力画像が、fal の動画生成で
422 `content_policy_violation`（"likenesses of real people"、`body.image_url`）として拒否された。
同じ Episode の地図・建物・行列の遠景、過去の「背後からの無名の人物」は通っている。
この工程のテストは「拒否されにくい映像案を Storyboard の時点で選ばせる」ことを固定する。

## domain の規則と prompt の構図（`tests/unit/test_storyboard_visual_subject.py`、unit）

規則は純粋関数、prompt 組み立ても純粋関数なので unit で固定する。provider は不要。

| テスト | 守るもの / 落ちたら何が起きているか |
|---|---|
| `test_the_single_generate_asset_names_the_visual_subject` | 語彙の全値が `required_assets` から読める（語彙の定義元は contracts の1箇所） |
| `test_assets_from_other_sources_do_not_count` | 生成しない素材（`provided` 等）は映像対象の宣言に数えない |
| `test_the_subject_must_be_decided_exactly_once` | 未決定・二重の宣言を推測で補わない。落ちれば「何を描くか」が決まらないシーンが画像生成に進む |
| `test_a_subject_outside_the_vocabulary_is_a_violation` | 自由文字列（例: 人名入りの型）を通さない |
| `test_people_subjects_are_the_named_and_the_anonymous_figure` | 人物の映像対象の集合（構図規則・prompt・復旧の検証が同じ集合を使う前提） |
| `test_a_person_cannot_be_planned_as_a_portrait_shot` | **事故の型そのもの**: 人物を `talking_head`/`character` で撮る計画を retryable な形式不正として LLM にやり直させる |
| `test_other_combinations_are_allowed` | 人物を一律に禁じていない（遠景・背後なら可）。落ちれば過剰な制約で storyboard が通らなくなる |
| `test_scenes_without_a_subject_keep_the_legacy_prompt_text` | 旧 storyboard のシーンは導入前と1文字も違わない文面で依頼される（2026-09-29 の出力を literal で固定） |
| `test_every_subject_adds_a_composition_instruction_to_the_image` | 映像対象を足したのに構図の指示が無い、という抜けを語彙全体で防ぐ |
| `test_people_are_drawn_small_and_not_as_a_recognizable_portrait` | 人物は小さく・肖像にしない構図で描かせる。**名前・史実の記述は消さない**ことも固定する（名前だけ消して判定をすり抜ける実装を防ぐ） |
| `test_non_people_subjects_do_not_add_people_instructions_to_the_video` | 人物のいないシーンの動画文面を変えない |
| `test_writing_is_never_baked_into_the_image` | `dedcf315` sb3 に "sohei" の文字が焼き込まれていた。文書・字幕背景でも読める文字を描かせない |
| `test_builder_versions_were_raised_for_the_subject_aware_rules` | 文面の規則を変えたら版を上げる（ADR-0017）。版の変更で成功済みシーンを作り直さないことは identity 側（ADR-0035 (4)）が保証する |

## adapter（`tests/unit/test_openmontage_storyboard_adapter.py`、unit）

| テスト | 守るもの |
|---|---|
| `test_storyboard_template_asks_for_one_visual_subject_per_scene` | テンプレートが語彙の全値・`required_assets`・拒否理由の根拠・「名前はナレーションで」を含む。語彙とテンプレートのずれを検出する |
| `test_interpret_maps_the_declared_visual_subject` | LLM 出力 → 下書き → `StoryboardScene` の形まで映像対象が落ちずに届く |
| `test_an_undecided_visual_subject_is_a_retryable_violation` | 欠落・空・語彙外・二重を `StoryboardSchemaViolationError`（retryable）にする |
| `test_a_named_person_planned_as_a_portrait_is_a_retryable_violation` | 規則が adapter の経路で実際に効いている |

既存テストの変更（仕様変更は ADR-0035 で承認）:

- 書き下ろしの最小 scene_plan スキーマに `required_assets` を足し、`_scene()` の既定に1件の宣言を足した
  （固定 commit の本物のスキーマには元々ある任意項目。新しい adapter はこれが無い出力を拒否する）。
- `test_storyboard_template_constants_and_rules`: テンプレート版 `2` → `3`。
- `test_style_profile_id_carries_the_builder_version` / `test_video_prompt_builder`: prompt 版 `v2` → `v3`。
