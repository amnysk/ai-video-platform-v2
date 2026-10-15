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
