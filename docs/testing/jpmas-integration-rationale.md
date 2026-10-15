# JP-MAS-001 §17 の実環境試験: テストの理由（`tests/integration/test_jpmas_intervention.py`）

JP-MAS-001 v2.0 §17 の必須検証 3・5・9 と INV-49 は、既存の単体試験（SQLite・time-skipping）にはあったが、
**実 PostgreSQL + 実 Temporal + 実 MinIO** で確かめた試験が無かった。ここはその穴を埋める。
隔離スタック（`deploy/logging/test/run-e2e.sh`）の中でだけ走り、本番・有料 provider・YouTube には触れない
（stage workflow は fake。DB は `*_test` の一時スキーマ、Temporal は `avp-test`、bucket は `artifacts-test`）。

| テスト | 守る契約・事故 | なぜ実環境か | 捕まえる失敗 |
|---|---|---|---|
| `test_a_resent_command_does_not_apply_or_dispatch_twice_on_a_real_stack` | §17-3: 同じ `command_id` の再送・別 workflow id での再実行・再配送で、変更も pipeline の start も増えない | 実 Temporal の履歴と実 DB の行を通して、`replayed` と start 回数 1 を見る | 再送のたびに control_revision が進む、`resume_requested` が重複する、子 pipeline が二重に start する |
| `test_a_crash_after_the_db_apply_recovers_to_one_change_on_a_real_stack` | §17-9 / INV-42: DB の apply が commit された後に workflow が落ちても、再確認で一度の変更に戻る | 「落ちた」を実 DB の commit 済み状態で再現し、実 workflow が保存済み result を読むことを見る | 再配送が再適用して revision が進む、pipeline が start されない／二重に start される |
| `test_concurrent_applies_of_one_command_change_the_database_once` | §17-3/9 の下地: 同じ command の apply が同時に走っても1回 | **SQLite は `FOR UPDATE` を無視する**。行ロックによる直列化は実 PostgreSQL でしか言えない | 行ロックの欠落で `resume_requested` が複数行になる、片方が例外になる |
| `test_a_received_hold_is_not_effective_and_an_effective_hold_dispatches_nothing` | §17-5: 停止の受付（`hold_requested`）と有効化（`held` / `hold.effective`）は別。有効化後は新規 stage を始めない | 受付と適用を別のトランザクションで実 DB に残し、実 Temporal 上の本物の `EpisodePipelineWorkflow` + control gate が `parked` で止まり、stage の workflow が1つも走らないことを見る | 受付だけで「停止済み」と扱う、有効化後にも stage を dispatch する |
| `test_a_retried_assistant_tool_call_is_one_logical_change_end_to_end` | INV-49: 同じ tool 呼び出しの再試行は同じ `command_id` で1つの変更。別内容での `tool_call_id` 再利用は拒否 | 実 API（実 `receive_command`・実 `TemporalCommandStarter`）に adapter を繋ぎ、実 workflow が適用するまで通す。再試行後も command 行・control_revision・イベントが増えない | adapter が再試行で別 `command_id` を作る、サーバが重複受付する、再試行が workflow を通して二重適用する |

## 競合（`tests/integration/test_jpmas_postgres_races.py`、実 PostgreSQL の別接続）

SQLite は `FOR UPDATE`/`FOR SHARE` を無視し接続も 1 本なので、競合の直列化は実 PostgreSQL でしか言えない。

| テスト | 守る契約・事故 | 捕まえる失敗 |
|---|---|---|
| `test_two_concurrent_applies_on_the_same_base_revision_give_one_applied_one_conflict` | §17-4 / INV-43: 同じ版への2つの採用が同時に走ると APPLIED 1・CONFLICT 1、現行版は勝者 | 両方が版を作る（lost update）、敗者が例外になる、change.applied が2回 |
| `test_a_human_and_an_ai_applying_the_same_base_concurrently_conflict_once` | §17-4: 人とAIの同時変更でも版を作るのは高々1つ | 主体ごとに競合判定が分かれる |
| `test_a_dispatch_in_flight_when_the_hold_is_received_is_listed_after_held` | INV-44: dispatch が行ロックを持つ間に来た hold の受付はその commit を待ち、HELD の一覧に載る | gate が制御行を `FOR SHARE` で読まない（変異で確認済み: 受付が先に commit され dispatch を取りこぼす） |
| `test_a_dispatch_after_the_hold_is_received_is_refused_and_unlisted` | INV-44: hold と dispatch を別接続で競合させ、結末は「送信済み・一覧にある」か「拒否・一覧に無い」だけ。HELD の後の dispatch は常に拒否 | 「両方」「どちらでもない」（課金されうる job が一覧から漏れる） |
| `test_a_legacy_episode_without_a_control_row_still_dispatches_on_postgres` | INV-44 の裏: 制御行の無い日次・旧方式は従来どおり dispatch できる | gate が旧方式を止める |

## 方針

- 単体試験の主張を**弱めず**、同じ主張を実環境で言い直す（既存テストは変更していない）。
- 再利用: `tests/unit/test_intervention_workflows.py` の fake stage・走り続ける fake pipeline、
  `tests/unit/test_assistant_e2e.py` の橋、`tests/support/intervention.py` の補助。定義を複製しない（AGENTS.md §8）。
- Worker は実 API の starter が使う固定 queue `intervention` を聞く。同じ namespace に並列で走らせない。
- 実行: `AVP_APPTEST_PROJECT=avp2-oslog-c* deploy/logging/test/run-e2e.sh run pytest tests/integration/test_jpmas_intervention.py`。
