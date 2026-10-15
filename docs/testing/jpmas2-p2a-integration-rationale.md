# JP-MAS-002 P2-A 統合時に追加したテストの理由

3 ブランチ（fixes / service / http）の merge 後、親エージェントの統合レビューで見つけた2件。

## tests/unit/test_application_http_starter_options.py

- `test_the_http_starter_uses_the_settings_pipeline_options`: HTTP の `start_agentic_production` が使う
  `LazyAgenticStarter` は、merge 前は `TemporalAgenticStarter(client)`（既定の `PipelineOptions`）を作っていた。
  ADR-0044 §3-2 の「API・worker・CLI で同じ options」が HTTP の新しい入口で崩れる（P1 既知問題 B の再発）。
  設定の並行数・render profile が `pipeline_options_from_settings` と同じ値で入力と task queue に載ることを固定する。

## tests/unit/test_application_service.py

- `test_a_resend_of_the_apply_that_decided_the_change_reaches_the_backend`: `apply_approved_change` の事前検査は
  change set が `proposed` 以外なら `invalid_state` を返す。適用が終わった後に**同じ command_id** を再送すると
  （UI の再接続・MCP の再試行）冪等な `created=False` ではなく `invalid_state` になっていた。
  `ChangeImpactPreview.decided_by_command_id`（追加、既定 None）が要求の command_id と一致するときだけ事前検査を
  省き、backend の command 冪等判定（INV-42）に渡す。別の command で決まった変更の再適用は従来どおり拒否する。

## 独立レビュー（Sonnet）の指摘に対して

- `test_an_ai_cannot_resume_an_agentic_episode_but_may_resume_a_legacy_one`（`tests/unit/test_application_service.py`）:
  `start_agentic_production` は人だけなのに、agentic Episode の `resume_production` は AI でも固定計画（有料）を
  歩かせられた。backend の「AI の有料再開」判定は開始工程が決まらない場合を覆わない。agentic の再開を人だけにし、
  legacy は従来どおり backend に任せることを固定する。
- `tests/unit/test_agentic_use_cases.py` の `TemporalAgenticStarter(wf_env.client)` → `(..., PipelineOptions())`:
  `options` を必須にした（既定値で起動する経路を型で無くす / ADR-0044 §3-2）ことに伴う呼び出しの書き換え。
  assertion は変えていない。

## 未開始の agentic draft の再開（`tests/unit/test_resume_unstarted_agentic_draft.py` ほか）

`CreateEpisodeDraft` は brief を持つが計画（`workflow_plan`）を pin しない。`read_pinned_agentic_template` は
これを legacy（`None`）と区別できず、resume が `EpisodePipelineWorkflow` を起動していた。正しい開始は
`start_agentic_production` だけ（ADR-0044 §3-4）。入口は3つあるので、それぞれを固定する。

- `test_helper_is_true_only_with_a_brief_and_no_plan`: 判定関数 `has_unstarted_agentic_draft` の真理値。
  brief なし（legacy）・brief のみ（draft）・brief+計画（開始済み）の3状態で、draft だけが True。
- `test_api_resume_of_an_unstarted_agentic_draft_is_409_and_starts_nothing`: `POST /episodes/{id}/resume` が
  409 `invalid_state` / `agentic_draft_not_started` で断り、agentic も legacy も起動しない。
- `test_api_resume_of_a_legacy_episode_still_starts_the_pipeline`: 判定が通常の legacy Episode の再開を
  巻き込まない（過剰拒否の防止）。
- `test_worker_resume_of_an_unstarted_agentic_draft_starts_nothing`: `/v1/intervention/.../commands` から直接
  送られた resume を worker の `check_start` が `invalid_state` で止める（flag に依存しない）。
- `test_resume_of_an_unstarted_agentic_draft_creates_no_command`（`test_application_service.py`）:
  `resume_production` が command を作る前に断り、観測に `invalid_state` を残す。
