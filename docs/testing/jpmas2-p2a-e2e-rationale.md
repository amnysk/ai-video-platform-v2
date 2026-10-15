# JP-MAS-002 P2-A HTTP E2E に追加したテストの理由

`tests/integration/test_jpmas2_p2a_http_e2e.py`。単体試験は route・service・reader を fake の port で見ている。
ここは **FastAPI app の入口から** 実 PostgreSQL・実 Temporal（namespace `avp-test`）・実 MinIO（`artifacts-test`）まで通し、
「層のつなぎ目」でしか起きない食い違いを見る。本物: route・認証（Bearer + principal registry）・`get_application_service`
の組み立て・`StoreBackedInterventionBackend`・`LazyAgenticStarter`・command workflow・`AgenticEpisodeWorkflow`。
差し替えは配線（session factory・store・Temporal 接続・settings・PipelineOptions の queue）と、P1-D と同じ fake の LLM・子 workflow だけ。

- `test_a_human_creates_a_draft_and_resending_it_returns_the_same_episode`: 201 -> 再送 200 で同じ episode_id、一覧・workspace に出る。
  同じ command_id で別内容は 409 `command_id_reused`。draft は何も走らせない（agent run 0）。pin 前は旧 pipeline の workflow id になることも固定。
- `test_start_production_runs_the_workflow_once_and_a_second_post_adds_nothing`: 人の起動 202 で workflow が実際に最後まで走り、
  2 回目は 200 `started=false`。予約・agent run・command・LLM 呼び出しが 1 件も増えない（二重課金なし）。
- `test_an_ai_assistant_cannot_start_production`: AI は 403 `forbidden`（`human_only`）。workflow も予約も作られない。
- `test_with_the_agentic_flag_off_...`: flag OFF の起動・再開は 404 `feature_disabled`。resume は command 行を作る前に断る
  （worker の無い workflow を投入しない / ADR-0044 §3）。workflow も立たない。
- `test_a_storyboard_proposal_is_recorded_as_proposed_and_does_not_apply`: 提案は 202、ChangeSet は `proposed`、現行の絵コンテは
  同じ revision のまま（提案と適用は別操作）。同じ提案の再送は 200 で command も ChangeSet も増えない。
- `test_a_storyboard_edit_that_needs_patch_scenes_is_unsupported`: `framing` は 422 `unsupported_change`、何も書かない。
- `test_apply_is_refused_before_any_command_when_the_approval_does_not_cover_the_cost`: QA の提案は実データで未見積り。
  承認額が 0 でも `cost_not_estimated`（未見積りを 0 円として通さない）、apply command は作られない。
- `test_a_paid_change_needs_the_estimate_and_an_ai_cannot_apply_it`: 見積り 0.30 USD の ChangeSet（store に seed。HTTP からは
  見積り付きを作れない）。承認 0.1 は `budget_exceeded` で command なし。AI は承認額が十分でも受付後に backend が
  `forbidden`（`ai_paid_apply`）で拒否し、ChangeSet・絵コンテ・予約は動かない。人が十分な額で承認する経路は有料の再生成を始めるので通さない。
- `test_hold_then_resume_are_accepted_once_and_resends_add_no_command`: hold・resume は 202、再送は 200 で command が増えない。
  agentic の resume 応答は `pipeline_kind=agentic` と `worker_check=pollers_present`。完了済みの再開で LLM・予約が増えない。
- `test_decision_quality_and_progress_reads_match_their_contract_models`: 3 つの読み取りが 200 で、結果 model で検証できる。
  未知の Episode は 404、認証なしは 401。
- `test_a_changed_creative_brief_makes_a_new_script_and_no_duplicate_reservation`: P1 既知問題 A を実 DB・実 bucket で。
  brief 変更後は新しい write_script job・新しい台本、旧台本は superseded の行として残り、同じ brief の再実行は予約を増やさない。
