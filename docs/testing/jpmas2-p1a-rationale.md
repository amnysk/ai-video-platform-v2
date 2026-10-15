# JP-MAS-002 P1-A（4 つの Agent handler）のテストの理由（ADR-0043 §6、INV-57）

テストを足した理由を、足したのと同じコミットで残す。全て `tests/unit/test_agent_handlers.py`。
fake LLM + 本物の `ToolGateway`・spec を使い、外部へは何も出ない。

## 1. 登録

- `test_handlers_register_every_handler_ref_and_only_those` /
  `test_every_spec_points_at_a_registered_handler_with_declared_inputs`: spec が指す handler が無いと実行時に
  初めて落ちる。`AgentHandlerRef` と `HANDLERS` を1対1に保つ。
- 各 happy path は「読んだ document が `AGENT_INPUT_NAMES` の範囲」「記録された tool が LLM だけ」も確かめる
  （handler が spec の外の入力・tool に触れない）。

## 2. Strategy

- `test_strategy_happy_path_builds_a_strategy_brief_and_decision`: 候補・推奨・判断記録（reported）の写像。
- `test_a_near_duplicate_of_a_recent_title_is_rejected_deterministically` /
  `test_a_rejected_recommendation_falls_back_to_the_first_proposed`: 重複はモデルの善意に任せず規則で落とす。
  LLM が重複候補を推奨しても採用されない。
- `test_an_era_outside_the_strategy_profile_is_rejected` / `test_no_surviving_candidate_is_an_output_rejection`:
  profile 外の era と、残る候補が無い出力を保存しない。
- `test_audience_basis_is_measured_only_with_enough_videos`: 米国20代を実測と偽らない（設計 §3）。実測フラグ
  だけでなく動画本数の下限も要る。仮定のときは不確実性の行が付く。
- `test_lessons_used_follows_status_not_the_llm`: validated だけが参考、他は仮説、rejected/retired は使わない。
- `test_a_brief_with_injection_text_changes_no_tool_call_and_is_only_data` /
  `test_a_prompt_cannot_close_its_data_block`: ブリーフ内の命令文が tool 呼び出しを変えない・DATA の区切りを
  閉じて指示を装えない。
- `test_a_strategy_llm_output_violating_the_contract_is_rejected`: slug 違反・候補数超過は `AgentOutputRejected`。

## 3. Creative

- `test_creative_copies_the_adopted_candidate_and_uses_the_first_hook`: topic/subject/angle/era は採用候補の写し、
  hook は先頭、must_keep は適用済み制約へ。
- `test_the_llm_cannot_change_the_adopted_subject_and_injection_is_inert`: LLM が subject などを返しても無視される。
- `test_an_unknown_or_rejected_adopted_candidate_is_refused_without_an_llm_call`: 採用候補が不正なら LLM を呼ばない。
- `test_creative_llm_output_violating_the_contract_is_rejected`: hook_options は 1〜3 個・長さ制約。

## 4. Quality

- `test_quality_pass_without_major_findings_has_no_proposals`: minor だけなら pass・提案なし・自己評価。
- `test_missing_input_blocks_the_review_without_an_llm_call`: 欠測を推測で埋めず LLM も呼ばない。
- `test_findings_on_unknown_scenes_or_beyond_the_duration_are_dropped_and_recorded`: 幻の scene・尺超過の所見は
  捨てるが黙って消さず判断記録の不確実性に残す。
- `test_major_findings_become_visual_only_proposals_capped_at_three` /
  `test_critical_findings_are_proposed_before_major_ones` /
  `test_a_major_finding_without_a_visual_fix_makes_no_proposal`: P0 が受け付ける visual_description だけ、最大
  3 edit、scene 重複なし、重大度順。提案であり適用しない（INV-57）。
- `test_rule_review_is_context_only_never_relabelled_as_model_findings`: 規則の所見をモデルの所見に付け替えない。
- `test_quality_llm_output_violating_the_contract_is_rejected`: severity/confidence/id 重複の違反。

## 5. Learning

- `test_learning_happy_path_proposes_a_lesson_with_measured_windows_copied`: 窓は入力の写し、欠測は null
  （0 にしない）、status は proposed、比較の基準を残す。
- `test_private_or_unmeasured_uploads_wait_and_never_call_the_llm`: 非公開・測定なしから Lesson を作らない。
- `test_a_small_sample_is_noted_and_status_stays_proposed`: 小標本は断定しない。
- `test_without_a_comparable_baseline_the_lesson_says_so` / `test_shorts_and_long_form_baselines_are_not_mixed`:
  比較できない基準で比較しない・形式を混ぜない（設計 §9）。
- `test_learning_llm_output_violating_the_contract_is_rejected` / `test_the_llm_cannot_set_the_lesson_status`:
  契約違反の拒否と、検証済みへの勝手な昇格の防止。

## 6. 既存テストの許可リスト変更（tests/architecture/test_single_change_path.py）

- `PASS_THROUGH` に `domain/agents/handlers/quality.py` を足した。Quality handler は提案（`StoryboardPatchProposal`）を
  **組み立てて返す**唯一の Agent で（設計 §3、ADR-0043 §6）、command への変換・command_id の導出は持たない。
  INV-50 の本旨（変換は `ProposeStoryboardChange` だけ）は他 2 テストが引き続き守る。テストは緩めず、許可対象を1つ足しただけ。
