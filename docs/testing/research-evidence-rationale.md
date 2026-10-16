# テスト設計の根拠: Evidence Research と台本の照合（ADR-0038、INV-36）

各テストが**なぜ必要なのか**を残す。B4 のテストが守るものは 5 つある。

1. 評価器（LLM）の呼び出しも台帳の枠を通る。予算外の呼び出しをしない（INV-36。旧実装の穴）。
2. 評価器の出力は提案で、コードの検査は評価を弱める方向にだけ働く（min 規則）。
3. 本文を完全に確認していない資料・作られた URL / 抜粋・評価していない claim から合格を作れない。
4. 評価器が無い・枠が尽きた・Provider が `none` のとき、結果は `partial` / `blocked` / `insufficient` で、
   `completed` / `passed` にならない（fail-closed）。
5. 照合結果は Evidence の依頼が所有する research の成果物で、壊れた Evidence では照合しない。

実際に起きた事故から作ったテストではない。旧ブランチ（`claude/research` 8cede47、旧 ADR-0032）の意味を
ADR-0037 の分離した永続化の上で作り直したときに生じた境界から作った。DB は SQLite、ArtifactStore は
インメモリ、Provider・評価器・抽出器は Fake だけ（固定コーパス）。実ネットワーク・有料 API には出ない。

## `tests/contract/test_research_evidence_contracts.py`（契約 = 最終防衛線）

| テスト | なぜ必要か / 落ちたら何が起きているか |
|---|---|
| `test_a_well_formed_artifact_round_trips` | 生成側（build）と取り込み側（parse）が同じ契約を通る。片側だけ変わると保存物が読めない |
| `test_an_unconfirmed_body_cannot_back_a_claim` | `truncated` / `failed` の資料が根拠になると、最後まで読んでいない本文で「確認済み」になる（旧実装は `truncated` を許していた） |
| `test_an_unassessed_claim_can_only_be_insufficient_and_has_no_links` | 評価器なし・枠切れの claim が `supported` に化ける経路を契約で塞ぐ |
| `test_insufficient_carries_no_usable_expression` | 根拠の無い主張に「使ってよい表現」が付くと台本がそれを事実として使える |
| `test_a_strong_claim_needs_two_independent_origins_and_an_authoritative_one` | 転載 2 件・ブログ 2 件で強い主張が `supported` にならないこと |
| `test_supported_with_a_refutation_is_rejected` | 反証のある主張を `supported` にしない（`disputed` にする） |
| `test_source_urls_must_be_plain_http_urls` | 認証情報つき URL・`javascript:` を資料として保存しない |
| `test_the_excerpt_limit_matches_the_passage_limit` | `domain` の passage 上限と契約の抜粋上限が食い違うと、正しい抜粋が契約で落ちる（同じ値を 2 か所に持つ理由は domain が契約の定数に依らず切るため） |
| `test_the_assessment_proposal_is_a_strict_llm_output_schema` | 実 LLM の strict な出力 schema にそのまま使える形（全 required・extra 禁止）であること |
| `test_passed_needs_completed_evidence_and_every_check_ok` | `partial` / `blocked` の Evidence・ok でない check・未登録の主張があるまま `passed` を作れない |
| `test_a_non_passing_verdict_needs_reasons_and_known_units` | 不合格の理由が空・台本に無い単位を指す照合結果を作らない |

## `tests/unit/test_evidence_domain.py`（純粋な規則）

| テスト | なぜ必要か |
|---|---|
| `test_excerpts_are_located_in_the_body_ignoring_width_and_spacing` | 抜粋の実在確認が全角半角・空白で誤判定すると、正しい根拠を捨てるか作られた抜粋を通す |
| `test_era_mismatch_and_quantifiers` | 年代のずれ・量化子の強さの検出が壊れると、1858 年の資料で 1868 年の主張を裏付けてしまう |
| `test_a_copied_body_shares_its_origin_and_other_sites_do_not` | 転載を独立した資料と数えない（強い主張の 2 origin 規則の前提） |
| `test_the_final_assessment_is_never_stronger_than_the_proposal` | min 規則。コードが提案より強い評価を作らない |
| `test_a_proposal_citing_an_unfetched_url_or_unknown_source_is_not_adopted` | 評価器が作った URL・資料を根拠にしない。その claim は「評価していない」になる |
| `test_a_fabricated_excerpt_is_dropped` | 本文に無い抜粋を根拠にしない |
| `test_a_truncated_source_is_never_a_basis` | 切り詰めの本文から link を作らない（契約より手前でも落とす） |
| `test_a_causal_claim_without_causal_wording_is_only_qualified` | 年表を並べただけの資料で因果の主張を `supported` にしない |
| `test_a_refutation_alongside_support_makes_the_claim_disputed_with_a_code_expression` | 異説の表現はコードが作る（評価器の断定表現を採らない） |
| `test_an_overstated_proposed_expression_is_replaced_by_an_attributed_one` | 資料より強い表現（「すべて」「常に」）を使ってよい表現にしない |
| `test_search_planning_is_deterministic_and_step_ids_name_their_claim` | 計画が揺れると再実行が別の呼び出しキーを作り枠を消費する。検索語に URL を作らない |

## `tests/unit/test_research_evidence_executor.py`（実行器 + EvidenceHandler + Fake）

| テスト | なぜ必要か / 落ちたら何が起きているか |
|---|---|
| `test_evidence_runs_search_fetch_and_assess_through_the_ledger` | 評価器の呼び出し回数 = 台帳の `assess` 行数 = 使用量。一致しなければ予算外の呼び出しがある（INV-36） |
| `test_a_rerun_reads_the_saved_proposal_and_does_not_call_the_assessor_again` | 確定の前に落ちた依頼の再実行が、評価を送り直さない（生データから読む） |
| `test_without_an_assessor_claims_stay_unassessed_and_the_request_is_partial` | 評価器が無いとき、呼ばずに `partial`（`assessor_not_available`）。合格にしない |
| `test_assessments_beyond_the_ceiling_are_not_sent_and_the_request_is_partial` | `max_assessments` を越えて呼ばない。枠外の claim は未評価のまま |
| `test_provider_none_is_blocked_without_any_call` | Provider `none` は台帳に 1 行も作らずに `blocked` |
| `test_a_bad_proposal_never_becomes_supported` | Fake の悪い提案 4 種（作った抜粋・未取得 URL・存在しない資料・強すぎる表現）が採用されない |
| `test_an_assessor_output_that_violates_the_schema_is_a_hole_not_a_pass` | schema 違反の出力は修復せず穴にする |
| `test_sources_use_the_fetched_final_url_and_unconfirmed_bodies_never_back_a_claim` | 資料の URL は転送後の `final_url`。検索結果の URL・snippet は載らない。passage は確認済みの本文だけから |
| `test_the_executor_rejects_evidence_sources_that_were_not_fetched` | Handler が URL を作っても実行器が保存しない（Handler を信用しない二重の検査） |
| `test_a_fresh_completed_evidence_request_is_reused_after_verification` | 鮮度内の同じ意味の Evidence は再利用し、`evidence_reverify_days` を過ぎたら再実行する |

## `tests/unit/test_script_verification.py`（照合の規則と `ScriptVerifier`）

| テスト | なぜ必要か |
|---|---|
| `test_a_script_within_the_evidence_passes` | 根拠の範囲内の台本が合格する（網が広すぎて何も通らない状態を検出する） |
| `test_an_overstated_sentence_fails` | 翻訳・短縮で「すべて」「必ず」に強まった文を落とす |
| `test_a_disputed_claim_stated_as_fact_fails` | 異説を断定で述べる文を落とす |
| `test_an_unregistered_claim_fails` | Evidence に無い年代・数量の主張を落とす |
| `test_relying_on_an_insufficient_claim_is_insufficient_not_passed` | 根拠不足の主張に依拠する台本は `insufficient`（合格でも台本の誤りでもない） |
| `test_evidence_that_did_not_complete_can_never_pass` | `partial` / `failed` の Evidence では合格しない |
| `test_extractor_candidates_are_added_to_the_deterministic_net` | 抽出器の候補は網に上乗せされる（網だけが拾わない文も検査する） |
| `test_the_evidence_must_belong_to_the_request` | 別の依頼の Evidence で照合しない |
| `test_the_verification_is_recorded_under_the_evidence_request` | 照合結果は Evidence の依頼の成果物として記録され、依頼の状態は変わらない。`episode_id` は参照として残る |
| `test_partial_evidence_without_an_assessor_verifies_as_insufficient` | 評価器なしの Evidence から合格が作れないことを、実行から照合まで通しで確かめる |
| `test_a_wrong_or_corrupted_evidence_artifact_is_not_verified` | 現行でない・sha256 が違う・読み戻しが一致しない Evidence では照合しない |

## `tests/unit/test_research_evidence_workflow.py`（registry + Workflow）

| テスト | なぜ必要か |
|---|---|
| `test_the_registry_builds_fake_llm_ports_only_for_fake` | `none` で評価器・抽出器が組まれない（fail-closed）。実 LLM が組まれない |
| `test_a_fake_worker_runs_evidence_to_a_completed_artifact` | worker の組み立て経路（registry）で Evidence が `handler_not_available` にならず、履歴に評価の件数と参照だけが載る |
| `test_an_assessment_ceiling_makes_the_workflow_partial` | Workflow 経由でも評価の枠が守られ、結果は `partial` |
