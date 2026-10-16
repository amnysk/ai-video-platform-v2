# テスト設計の根拠: Research の Workflow・worker・起動・API（ADR-0037 §8.5、INV-36 / INV-37）

各テストが**なぜ必要なのか**を残す。B3 のテストが守るものは 5 つある。

1. Workflow は決定的で、履歴には参照と件数だけが載る（replay で壊れない。ADR-0029）。
2. retry は上限つきで、retry も呼び出しの枠を数える。retry しない失敗は retry しない（INV-36、§8.4）。
3. 設定が無い既定の状態（`RESEARCH_PROVIDER=none`）では何も外に出ない（fail-closed。§6）。
4. 同じ依頼を二重に走らせない。再開した依頼は必ずもう一度走れる。
5. Episode の日次・pipeline・企画・台本は Research を起動しない・待たない（INV-37）。

実際に起きた事故から作ったテストではない。旧ブランチ（`claude/research` 8cede47）の Workflow・worker・API の
意味を、B2 の 1 本の `ResearchExecutor.execute` の上で作り直したときに生じた境界から作った。そのため、各テストに
ついて「落ちたら何が起きているか」を書く。Temporal は time-skipping のテストサーバ、DB は SQLite、
ArtifactStore はインメモリ、Provider は Fake だけ。実ネットワーク・有料 API・compose には出ない（AGENTS.md §9）。

## `tests/unit/test_research_workflow.py`（Workflow + 本物の Activity + 本物の executor）

| テスト | なぜ必要か / 落ちたら何が起きているか |
|---|---|
| `test_a_queued_request_completes_and_the_history_carries_only_refs_and_counts` | 端から端まで（worker 登録 → Activity → executor → DB）が繋がっていること。履歴の Activity の結果を復号し、`ResearchWorkflowOutput` の欄と成果物の参照（型・id・sha256）以外が無いことを見る。検索結果や本文が履歴に載ると、履歴の上限と replay の安定性を壊し、成果物の正本が 2 つになる |
| `test_re_running_the_workflow_for_a_finished_request_makes_no_new_calls` | 同じ依頼の workflow が 2 回走っても（起動は `ALLOW_DUPLICATE`）、外部を呼び直さず台帳も増えない。起動の方針（§8.5）が安全なのはこの冪等性に依存する |
| `test_provider_none_blocks_the_request_without_any_call` | Provider が無い worker は外部を呼ばずに `blocked`（`provider_not_configured`）。台帳に行が 1 つでもあれば、設定の無い環境で呼び出しを予約したことになる |
| `test_the_default_worker_settings_build_a_worker_that_blocks` | `run_worker.build_activities` を既定の設定で組むと、上と同じく `blocked` になる。registry を経由した本番の組み立て経路で fail-closed を確かめる（テスト用の Provider 注入だけでは経路の取り違えを見逃す） |
| `test_a_transient_failure_that_recovers_is_retried_and_completes` | 一時障害は Activity の retry で回復し、成功済みの検索は送り直さない |
| `test_transient_failures_are_retried_a_bounded_number_of_times_then_recorded_failed` | 一時障害が続いても試行は `RESEARCH_EXECUTE_MAX_ATTEMPTS` 回で止まり、`failed`（`execution_failed`、型名つき）が DB に残る。台帳の行数が「成功 1 + 試行回数」であることで、retry も枠を数えることを固定する。無限 retry すると依頼が終わらず、枠も使い切る |
| `test_a_non_retryable_research_error_is_not_retried_and_is_recorded` | research の型（`domain/research/errors.py`）は基底の表に載らない。Worker が research の表を合わせて渡していないと、permanent / needs_input の research の例外を Temporal が retry する（§8.4 で書いた負債の検査）。needs_input は `blocked`、permanent は `failed` |
| `test_the_execute_retry_policy_is_bounded_and_uses_both_error_tables` | RetryPolicy の上限と non-retryable の集合が `RESEARCH_WORKER_NON_RETRYABLE_ERROR_TYPE_NAMES`（基底 ∪ research）であり、retryable な `ResearchSourceUnavailableError` が入っていないこと。入っていると一時障害を retry せずに失敗にする |
| `test_the_worker_listens_on_the_contract_queue` | worker の queue が `contracts/research.py` の唯一の定義であること。starter と worker の queue が食い違うと、依頼は起動されても誰も拾わない |

## `tests/unit/test_research_activity_payloads.py`（ADR-0029 の境界）

| テスト | なぜ必要か |
|---|---|
| `test_every_boundary_type_roundtrips_through_the_default_converter` | Temporal の既定 converter は型注釈で復号する。送る側は成功しても復号する側（workflow）だけが失敗し、workflow task が無限に再試行される（ADR-0029 の事故の型）。実際の形（非空の参照・金額の文字列）で往復させる |
| `test_every_boundary_dataclass_in_the_contract_is_sampled` | 境界の dataclass を足したときに見本の足し忘れを検出する |
| `test_the_output_carries_only_refs_and_counts` | 結果の欄を固定する。本体を載せる欄を足すと、この検査の更新が要る（履歴に本体を載せない判断を黙って変えられない） |
| `test_the_executor_outcome_maps_onto_the_boundary_shape` | `ResearchExecution` から境界の形への写し（enum → 値、`Decimal` → 文字列）。`Decimal` をそのまま入れると既定 converter で往復しない |
| `test_the_workflow_id_is_one_per_request_and_rejects_non_uuids` | workflow id は依頼 1 件につき 1 つ。正準でない UUID を受けると、同じ依頼に 2 つの id ができて二重実行の防止（Temporal の id 一意性）が効かない |

## `tests/unit/test_research_api.py`（API と起動）

| テスト | なぜ必要か |
|---|---|
| `test_submitting_a_request_stores_it_and_starts_the_workflow_without_waiting` | 受け付け → `queued` → 起動 → 202。完了は待たない（INV-16） |
| `test_resubmitting_the_same_key_returns_the_same_request_and_restarts_it` | 保存と起動の間で api が落ちた依頼の回収経路。同じキーの再 POST が同じ依頼・同じ workflow id で起動し直す |
| `test_the_same_key_with_a_different_meaning_is_a_conflict` | 冪等キーの取り違えを 409 で返し、起動しない |
| `test_an_invalid_request_is_rejected_before_anything_is_stored` | 種別をまたぐ入力（Trend の入力を Evidence に）は 422 で、保存も起動もしない |
| `test_provider_none_blocks_at_submit_and_starts_nothing` | 既定の `none` では受け付けの時点で `blocked` になり、workflow を起動しない。理由コードが `blocked_reason` と `result` の両方に残る |
| `test_a_fresh_completed_request_is_reused_without_starting_a_workflow` | 鮮度内の完了済み依頼（本体の検証つき）を再利用したときは、新しく起動しない（二重の調査・二重の支出をしない） |
| `test_resume_of_a_request_still_without_provider_is_a_conflict` | 門を通らない再開は 409 で何も変えない（再開しても同じ理由で止まるだけ） |
| `test_resume_after_the_provider_is_configured_requeues_and_starts` | 門を通った再開だけが `queued` に戻して起動する。2 回目の再開は 409（二重に起動しない） |
| `test_resume_and_get_of_an_unknown_request_are_404` | 未知の依頼は起動しない |
| `test_get_reads_the_database_only` | GET は起動もせず、台帳も作らない（INV-8） |
| `test_the_starter_uses_one_workflow_id_per_request_on_the_research_queue` | 起動の id・queue・id 再利用の方針（`ALLOW_DUPLICATE`）。`ALLOW_DUPLICATE_FAILED_ONLY` に戻すと、`blocked` で成功終了した依頼を再開しても二度と走らない（旧実装が別経路で回避していた問題） |
| `test_an_already_running_workflow_is_not_started_twice_and_is_not_an_error` | 実行中の id の拒否は成功扱い（同じ依頼を同時に 2 つ走らせない。API は 500 を返さない） |
| `test_the_research_router_only_adds_paths_under_research` | Research の API は `/research` の下だけに足す。`POST /episodes/{id}/script`（ADR-0037 §9 で移植しない）を作らない |

## `tests/contract/test_research_worker_compose.py`（compose）

| テスト | なぜ必要か |
|---|---|
| `test_the_research_worker_env_is_app_env_plus_the_provider_only` | research-worker に YouTube / Codex / fal の資格情報を渡すと、実 Provider の配線（所有者の判断と ADR を待つ）を設定だけで迂回できる |
| `test_the_api_reads_the_same_provider_setting` / `test_the_provider_is_only_given_to_the_api_and_the_research_worker` | 受け付けの門と実行の門が同じ変数・同じ既定（`none`）を読むこと。api に渡し忘れると、`.env` を `fake` にしても受け付けで常に `blocked` になる（旧ブランチの compose はこの状態だった） |
| `test_the_research_worker_has_no_codex_sandbox_exceptions` | Codex を使わない worker に sandbox の権限（SETFCAP・seccomp 解除）を与えない |
| `test_env_example_documents_the_fail_closed_default` / `test_the_default_provider_is_none` | 手順書どおりに `.env` を作ると既定が `none` になること |

一般の Worker の契約（command・healthcheck の queue・restart・depends_on・単一イメージ・deploy の集合）は、既存の
`tests/contract/test_compose_workers.py` の `WORKERS` と `tests/unit/test_deploy_scripts.py` の `APP` に
`research-worker` を足して検査する（ADR-0037 §8.5 が承認した、列挙への追加だけの変更）。

## アーキテクチャテスト

| テスト | なぜ必要か |
|---|---|
| `tests/architecture/test_research_workflow_determinism.py` | Workflow が DB・HTTP・乱数・壁時計・`infrastructure` を使うと replay で履歴と食い違い、走行中の依頼が壊れる。Activity は名前で呼ぶ（INV-3）。全ての Activity 呼び出しに timeout と retry policy がある（無限 retry をしない）。旧 `test_research_workflow_determinism.py` の意味の移植 |
| `tests/architecture/test_daily_does_not_wait_for_research.py` | 日次・pipeline・企画・台本の workflow と worker が Research の workflow・queue・Activity 名・起動・Gateway・実行器を（import でも文字列でも）名指さない。名指しが無ければ、Research が止まっても日次は待たず失敗もしない（INV-37）。Research の workflow を登録する worker は research-worker だけ。旧 `test_daily_does_not_wait_for_research.py` の意味の移植（Trend の定期更新と `require_fresh` は移植していないので、その検査は無い） |
| `tests/architecture/test_research_isolation.py`（検査対象の追加） | 起動（`infrastructure/temporal/research_starter.py`）と API のルータも Research のコードとして、本番の表・課金コードを import しない検査と、本番の工程から import されない検査の対象にする |
