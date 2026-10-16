# JP-MAS-002 P1-D: 実環境 E2E と、そこで見つかった欠陥のテストの理由

P1 の単体試験は SQLite・time-skipping・fake の子 workflow で組まれていた。実 PostgreSQL + 実 Temporal + 実 MinIO
（隔離スタック `deploy/logging/test/run-e2e.sh`）で 1 Episode を通すと、SQLite では見えない欠陥が出る。

## 実環境で見つかった欠陥（単体）

| テスト | 守る契約・事故 | 捕まえる失敗 |
|---|---|---|
| `tests/unit/test_agent_reservation_key.py::test_the_stage_key_itself_does_not_fit_the_reservation_column` | 前提の固定: Activity の冪等キーは台帳の列（VARCHAR(64)）より長い | 前提が変わった（キーが短くなった）のに写しが残る |
| `tests/unit/test_agent_reservation_key.py::test_the_reservation_key_fits_the_column_and_is_deterministic_per_stage_key` | INV-56: 台帳の key は列に収まり、stage ごとに決定的で別の stage と衝突しない | 実 PostgreSQL で `StringDataRightTruncation`（全 agent stage が予約できず止まる）。SQLite は長さを見ない |
| `tests/unit/test_agent_reservation_key.py::test_a_reservation_for_a_real_sized_stage_key_is_found_again_by_the_mapped_key` | INV-56: 写した key でも再送は同じ予約（二重課金しない）で、`find_by_key` で引き直せる | 写しを片側（予約）だけに適用して再送が別予約になる |
| `tests/unit/test_agent_stage_scope.py::test_the_key_without_a_scope_is_unchanged_and_a_scope_makes_a_different_key` | 本流の stage の冪等キーは従来どおり（replay・予約の互換）。scope を足すと別の実行になる | scope の導入で本流のキーが変わり、保存済みの結果と台帳を引けなくなる |
| `tests/unit/test_agent_stage_scope.py::test_a_later_run_with_a_new_scope_can_produce_the_lesson_after_an_awaiting_run` | 設計 §9: 測定が揃うまで Learning を何度でも起動できる。同じ起動の再試行は replay、新しい起動は測り直す | `awaiting_public_metrics` が冪等キーで replay され続け、公開後でも Lesson ができない／scope を足したのに再試行が二重に走る |

## 実環境 E2E（`tests/integration/test_jpmas2_agentic_e2e.py`）

隔離スタック（実 PostgreSQL・実 Temporal・実 MinIO）の中でだけ走る。本物: `AgenticEpisodeWorkflow`・pin / 採用 / control gate /
upload gate の Activity・`run_agent_stage` と本物の `HANDLERS`・永続化・`ProposeStoryboardChange` + `SessionCommandPort` +
実 `InterventionCommandWorkflow`。偽物: LLM（`FakeAgentLlm` + 定型応答）、既存工程の子 workflow（script / storyboard /
production / upload。render は本物の `RenderActivities` + fake engine）、公開後の測定（`MockPublicMetrics`）。有料 API・Codex・
YouTube・fal には出ない。スキーマは使い捨て、Temporal は `avp-test`、bucket は `artifacts-test`。

| テスト | 守る契約・事故 | なぜ実環境か | 捕まえる失敗 |
|---|---|---|---|
| `test_one_episode_runs_every_stage_on_the_real_stack` | P1 完了条件: draft から private upload・品質所見まで 1 Episode が template の順に通る。現行 Artifact は種類ごとに 1 つ、3 role の agent_runs（model は fake、費用は NULL）、判断記録（producer は `agent:producer`）、topic = 採用候補、TopicPlan・日次枠なし、所見は注記、修正は agent の PROPOSED のまま、予約は上限で数える（INV-50/56/57） | 列長・FK・JSON・Numeric は SQLite と違う。実 Temporal の履歴と queue を通す。実際に 64 文字超の予約 key で全 stage が止まる欠陥を見つけた | stage の順序違い、成果物の二重現行、費用を 0 で埋める、producer が TopicPlan・日次枠を使う、QA の提案が適用される |
| `test_learning_waits_for_public_metrics_then_proposes_one_lesson` | 設計 §9: 公開前は `awaiting_public_metrics`（Lesson なし）、公開後に同じ Episode で起動し直せば proposed の Lesson が 1 つ（INV-57） | 冪等キーが同じ起動の間 replay される実装は、実 Temporal で 2 回起動して初めて露見した | awaiting が replay され続ける、Lesson が proposed 以外、小標本の注記が無い |
| `test_a_replayed_run_adds_no_runs_reservations_artifacts_or_proposals` | 二重課金しない: 同じ Episode の再実行・同じ冪等キーの Activity 再送で、LLM 呼び出し・予約・成果物・提案・判断・注記が 1 件も増えない。提案の再送は同じ command 群（INV-50/56） | 実 PostgreSQL の一意制約と実 Temporal の再実行を通す | 再実行で agent_runs / 予約 / revision / ChangeSet が増える、再送が別の command になる |
| `test_a_pinned_plan_survives_a_changed_template` | INV-51: pin した計画は Template が変わっても同じ `plan_hash` で使われる | 実 DB の `workflow_plan` と実 Temporal の履歴で確かめる | 実行のたびに再コンパイルして計画が変わる |
| `test_a_drifted_agent_spec_stops_with_one_attempt` | INV-51: pin 後に spec の hash が変わると `agent_spec_drift` で止まり、`retry_max_attempts=3` でも試行は 1 回、LLM・予約・成果物なし | 実 Temporal の retry policy（非再試行 type）が効くことは実環境でだけ言える | drift が再試行される、drift でも LLM が呼ばれる |
| `test_a_hold_requested_before_start_parks_at_the_first_stage` | 停止中の Episode は最初の agent stage を起動しない（parked）。予約・LLM・成果物なし | 実 `receive_command` / `apply_command` で HELD にし、実 control gate を通す | 停止中でも agent の有料呼び出しを始める |
| `test_a_missing_exchange_rate_blocks_the_first_agent_stage` | INV-56: `BUDGET_USD_JPY_RATE` が無いと `budget_unverifiable` で止まり、LLM・予約・成果物を作らない | 実 DB の予算ビューと実 workflow の停止理由を通す | 換算率が無いのに予算を 0 とみなして呼ぶ |
| `test_the_default_pipeline_worker_is_unchanged_and_a_legacy_run_writes_nothing_agentic` | INV-55: flag 既定の pipeline worker は agentic を登録せず、旧 `EpisodePipelineWorkflow` の実行は従来どおり完走し、新 role の agent_runs・新 Artifact・agent_llm 予約を作らない | 同じスタックで agentic worker と旧 worker を並べても日次が汚れない | 既定登録に agentic が混ざる、旧実行が agentic の行を書く |

### 偽物の既存工程（`tests/support/agentic_e2e.py`）

- 子 workflow は Activity を 1 つ呼ぶだけ。Activity が次の工程が読む現行 Artifact と Episode の状態を書く（再実行しても増やさない）。
  script / storyboard は `INV-46` のため、本物の工程が残す生成記録の代わりに decision_record も残す（無いと auto_intervenable の
  Episode は quality の有料 dispatch を拒否される）。
