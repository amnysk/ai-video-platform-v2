# ログ基盤（ADR-0040）の検証計画

担当C の試験台本。**本番（compose project `avp2`・`avp2-logging`）では実行しない。** 全て隔離環境
（app: `avp2-oslog-c*`、ログ基盤: `avp2-logging-c*`）で行う。有料 provider・YouTube は fake のみ
（隔離 app スタックの network は `internal: true` で外へ出られない）。

- 部品の置き場と理由: `deploy/logging/test/`、[docs/testing/logging-verification-rationale.md](../testing/logging-verification-rationale.md)
- 運用手順: [docs/operations/logging-runbook.md](../operations/logging-runbook.md)
- 依存の略記: **A** = アプリのログ実装（`infrastructure/logging`・発行位置）、**B** = Fluent Bit /
  OpenSearch / Dashboards（`deploy/logging/`）、**C** = この文書の部品

> 合否は DB・Temporal・OpenSearch への問い合わせと Fluent Bit の metrics で機械的に判定する。
> ログの件数で課金・再実行を判断しない（INV-38）。「ログに無い」は未実行の証明にならない。

## 0. 共通の準備

```bash
cd .worktrees/claude-oslog-verify            # A・B を merge した後のこの branch
T=deploy/logging/test
$T/run-e2e.sh build                          # worker イメージ（別 tag）+ test-runner
$T/run-e2e.sh up                             # 隔離 postgres/temporal/minio + namespace + migrate
# B: ログ基盤を別 project で起動（B の手順に従う。env 名は B の成果に合わせる）
export LOGGING_PROJECT=avp2-logging-c LOGGING_COMPOSE=deploy/logging/compose.logging.yaml
export OPENSEARCH_URL=https://127.0.0.1:<B の test 用ポート> OPENSEARCH_CA=<CA のパス>
export OPENSEARCH_USER=avp_log_viewer OPENSEARCH_PASSWORD_FILE=<0600 のファイル>
export OPENSEARCH_INDEX='avp-app-test-*'
PY=.venv/bin/python; SA="$PY $T/search-assert.py"
```

隔離 app スタックの実績（フェーズ1、A・B 実装前のコード）は §4。

## 1. 必須試験

| ID | 項目 | 手順 | 合否基準 | 再現コマンド | 依存 |
|---|---|---|---|---|---|
| S-E2E | 通常の Episode 経路 | 隔離スタックで既存 e2e（§3 の integration 一覧）を JSON ログ有効で実行し、1 Episode を検索 | 既存テストが全て pass（ログ無効時と同数）。その episode_id の `stage.*`・`activity.*`・`provider.call.*`・`reservation.*`・`artifact.stored`・`render.validation.*`・`upload.*` が揃い、全文書が `REQUIRED_APP_FIELDS` を持つ。`provider_request_id`・`reservation_id` は取得後の行にだけある | `$T/run-e2e.sh run pytest tests/integration/test_scene_rejection_recovery_e2e.py -p no:cacheprovider -p apptest_logging_plugin -s -q`、`$SA fields --term episode_id=<id> --require-contract`、`$SA count --term episode_id=<id> --term event_name=upload.succeeded --expect 1` | A（configure_logging・interceptor・発行位置）、B（経路） |
| S-CTX | 同時 Episode の context 分離 | 2 Episode を並行（`test_upload_workflow_persistence.py::test_concurrent_double_start…`、`test_daily_slot_concurrency.py`、A の unit test の並列 Activity）で走らせる | ある Activity の文書の `episode_id` が、その Activity の入力の episode と常に一致（`$SA distinct --term activity_id=<x> --field episode_id` が1値）。Activity 終了後の `log.record` に前の episode_id が残らない | 上記 + A の unit test（contextvars の解除） | A |
| S-REPLAY | Workflow replay | e2e 実行後の履歴を `Replayer` で replay（A の unit test）、および worker を SIGKILL して別 worker が引き継ぐ（`test_worker_restart_durability.py`） | replay で `stage.*` が再発行されない（`$SA dupes` 0、`stage.started` の件数が workflow run ごとに1）。非決定エラー 0。`event_id` の導出が決定的 | `$T/run-e2e.sh run pytest tests/integration/test_worker_restart_durability.py …`、A の replay unit test | A |
| S-403 | 403 と fallback の照合 | `test_incident_recovery_e2e.py::test_sb6_403_blocks…`・`::test_auth_incident_threshold…` | `provider.call.failed` に `http_status=403`・`error_category=access_denied`・`classification_basis=http_status_only`（credentials と断定しない）、`provider.auth_incident.recorded` が `provider_auth_incidents` の行数と一致。`provider.call.suppressed` は抑止された submit 数と一致。再開後 sb6 だけ新規 submit | `$SA count --term episode_id=<id> --term http_status=403 --min 1` と `SELECT count(*) FROM provider_auth_incidents WHERE …` の突き合わせ | A |
| S-422 | 422 / file_download_error と fallback | `test_scene_rejection_recovery_e2e.py`、`test_input_fetch_retry_e2e.py` 全4件、`test_incident_recovery_e2e.py::test_422…` | `scene.rejected` の件数・`scene_id`・`error_code`（配列）・`error_category` が `provider_rejections` の行と一致。`scene.alternative.started/result` が代替案 Artifact と一致、`scene_revision` が override の revision。`scene.input_refetch` が上げ直しの回数と一致。分類不能 422 は planner のイベント 0 | `$SA count --term episode_id=<id> --term event_name=scene.rejected --expect <DB 行数>` | A |
| S-REUSE | 成功済み scene の再利用 | `test_production_e2e.py::test_production_end_to_end_then_rerun_reuses_everything`、`test_scene_rejection_recovery_e2e.py`（sb1〜sb5） | rerun で `artifact.reused` が再利用シーン数だけあり、そのシーンの `provider.call.*`（submit）が 0。`artifact.reuse_rejected`（破損）は `test_corrupt_artifact…` でのみ | `$SA count --term episode_id=<id> --term event_name=artifact.reused --expect N` | A |
| S-RESUME | resume 時の課金・投稿安全性 | `test_episode_resume.py` 全4件、`test_production_e2e.py::test_rerun_from_blocked…`、`test_input_fetch_retry_e2e.py::test_second_input_fetch_failure…`、`test_upload_workflow_persistence.py` 全4件 | **既存テストの合否が不変**（JSON ログ有効／`AVP_LOG_FORMAT=text`／A のロガー故障注入の3条件で同じ結果）。`episode.resume.*` が POST 回数と一致、未照合予約の再送 0（`reservation.blocked`）、`upload.succeeded` は1回、再実行は `upload.reused_existing` | 3条件で `$T/run-e2e.sh run pytest <上記>` を回し結果件数を比較 | A |
| S-SEC | 秘密の非漏洩（stdout・Docker ログ・Fluent Bit バッファ・検索結果） | `fault-secrets.sh` | logging 経由の needle が json-file・buffer・OpenSearch（app / infra 両系統の `_source` 全走査）で 0 件。隔離スタックの実パスワード（env）も 0 件。`redaction_applied=true` が付く。`--raw`（logging を通らない print）は OpenSearch で B の Lua の対象パターン分 0 件（残った種類は ADR の「残るリスク」として報告） | `$T/fault-secrets.sh` | A（整形器）、B（Lua・buffer volume 名） |
| S-STOP | 停止・復旧・再起動 | `fault-opensearch-stop.sh 120`、Fluent Bit の `docker restart`、隔離 app の worker SIGKILL | 停止中も loggen・既存テストが遅延なく完了（業務の所要時間が停止無しと同程度）。復旧後に全件・重複 0。`retries_failed_total` 増えない | `$T/fault-opensearch-stop.sh 120 3000` | B |
| S-ROT | rotation（Collector 停止中） | `APPTEST_LOG_MAX_SIZE=1m APPTEST_LOG_MAX_FILE=5` で app を起動し `fault-collector-rotation.sh` | 一巡しない量: 全件・重複 0。一巡させる量: 欠損が出る（件数 < 出力）ことを記録し、`check-pipeline.sh` / runbook の追いつき確認で事前に検知できること | `$T/fault-collector-rotation.sh 20000 200` | B |
| S-DUP | 再送重複（event_id） | `loggen.py --duplicate-every 10`、OpenSearch を Bulk 中に停止→再開、Fluent Bit の位置 DB を消して再読（隔離 volume のみ） | 同一 index 内では `$SA dupes --expect 0`。rollover をまたぐ再送は重複し得る（件数を記録し、検索例の event_id 重複除去で吸収できること） | `$T/run-e2e.sh tool python $T/loggen.py --tag d1 --count 1000 --duplicate-every 10` → `$SA count --term request_id=d1 --expect 1000` | B |
| S-CAP | 容量上限・破棄 | B の storage を tmpfs（例 64MiB、`storage.total_limit_size` 16MiB）にして `fault-capacity.sh` | ホストの `/` 使用量が変わらない。上限到達で `dropped_records_total`（または storage の破棄 metrics）が増え、アプリ側は止まらない。復旧後は新しい側が届く | `$T/fault-capacity.sh 200000 400` | B（tmpfs 差し替えの env） |
| S-BAD | 不正 JSON・型不整合・Bulk 部分失敗 | `fault-bad-lines.sh` | 正常行は全件。型不整合は `collector_errors`（`@timestamp_replaced` 等）と `attributes.collector_moved`。不正 JSON は infra 系統（`log_source=unstructured`）。長大行は skip され metrics に出る。後続行が詰まらない | `$T/fault-bad-lines.sh 500 40000` | B |
| S-IDX | alias・ISM・bootstrap 再実行 | 短縮 policy（例 rollover `min_doc_count` 50、delete `min_index_age` 5m、`plugins.index_state_management.job_interval=1`）で bootstrap → loggen で rollover させる → bootstrap を再実行 | `$SA alias --alias avp-app-test-write` が write index 1つで、rollover 後に `-000002` へ移る。`$SA ism --index 'avp-app-test-*' --policy <名>` で全 index が管理下・failed 無し、delete 後に旧 index が消える。bootstrap 前後の `$SA snapshot` の diff が空（index 以外）。alias 名の実 index 化が拒否される（`PUT avp-app-test-write/_doc` が 404/403） | `$SA snapshot --out a.json; <bootstrap>; $SA snapshot --out b.json; diff a.json b.json` | B（短縮 policy・job_interval の差し替え手段） |
| S-VER | version 一致 | OpenSearch・Dashboards・Fluent Bit の稼働版と digest | `$SA version --expect 3.8.0`、Dashboards の `/api/status` の版、`fluent-bit --version` = 5.1.2、各 image digest が `deploy/logging/VERSIONS.md` と一致 | `docker inspect --format '{{.Image}}'` と VERSIONS.md の突き合わせ | B |
| S-RES | 資源測定 | 待機・e2e 実行中・障害注入中に `measure.sh` | OpenSearch の RSS ≤ mem_limit、swap を使わない（`memswap_limit`）、MemAvailable の推移を記録し ADR §7 の閾値（2GiB / 1.5GiB）判断材料にする | `$T/measure.sh "$XDG_RUNTIME_DIR/m.csv" 30 10` | B |
| S-RB | 停止・rollback 手順 | runbook §8 の3段（avp2-logging-c を止める／`AVP_LOG_FORMAT=text`／`logging:` を戻す）を隔離環境で実施 | 各段でアプリの既存テストが pass のまま。text 形式で出力が従来形式に戻る。ログ基盤を `down -v` してもアプリに影響なし | `APPTEST_LOG_FORMAT=text $T/run-e2e.sh run pytest tests/integration/test_production_e2e.py …` | A（text 切替）、B |

## 2. 前提として確認すること（フェーズ2 の開始条件）

1. A: `infrastructure.logging.configure_logging()` が引数無しで呼べる（引数が要るなら
   `apptest_logging_plugin.py` と `inject-secrets.py` を合わせる）。
2. A: Activity interceptor が**既存 integration テストが組む `Worker(...)`** にも入るか。
   テストが自前で `Worker` を組む場合、interceptor 無しでは `activity.*` と文脈（episode_id 等）が
   出ない。入れる手段（共通 factory、または plugin で worker 生成を包む）を A と決める。
   テストの中身は変えない。
3. A: durability 系（`tests/support/durability/common.py`）の worker 子プロセスは stdout を
   **ファイル**に向けている。そのログは json-file に載らない（S-REPLAY はログ件数ではなく
   テスト合否と A の replay unit test で見る）。
4. B: test 用の env 差し替え（env 名 `test`、containers dir、対象 compose project の完全一致に
   `avp2-oslog-c`、ホストポート、volume 名、storage を tmpfs にする手段、短縮 ISM policy・
   `job_interval`）と、viewer ユーザーの資格情報ファイルの置き場。
5. B: Fluent Bit の metrics で buffer 破棄・再送・long line skip を読む名前（`lib.sh` の `fb_metrics`
   の grep を合わせる）。

## 3. 既存回帰の実行対象

ログ実装は既存の制御を変えない（INV-38）。次を**ログ有効（json）・text・ロガー故障注入**の3条件で
回し、結果件数が一致することを確認する。

### unit / contract / architecture（ホストの .venv、インフラ不要）

```bash
.venv/bin/pytest tests/unit tests/contract tests/architecture -q
```

特に見る（領域別）:

| 領域 | ファイル |
|---|---|
| 課金・予約台帳 | `tests/unit/test_paid_job.py`、`test_provider_reservations.py`、`test_production_repositories.py`、`test_production_foundation.py`、`test_research_call_ledger.py` |
| 再試行・失敗分類 | `test_activity_errors.py`、`test_failure_class_registry.py`、`test_failure_policy.py`、`test_fal_queue.py`、`test_fal_storage.py`、`test_temporal_connect_retry.py` |
| 422 対策・シーン復旧 | `test_production_scene_recovery_workflow.py`、`test_scene_alternative_activity.py`、`test_scene_alternative_rules.py`、`test_fal_seedance_video.py`、`test_fal_seedream_image.py`、`test_production_video_activities.py`、`test_production_image_activities.py` |
| scene identity・再利用 | `test_scene_identity_reuse.py`、`test_scene_identity_v2.py`、`test_production_identity.py`、`test_storyboard_identity.py`、`test_artifact_verification.py`、`test_artifact_verify_io.py`、`test_artifact_generations.py` |
| resume | `test_resume_api.py`、`test_resume_plan.py`、`test_episode_diagnostic.py`、`test_daily_watchdog.py`、`test_daily_watchdog_workflow.py` |
| 投稿 | `test_upload_activities.py`、`test_upload_workflow.py`、`test_upload_workflow_e2e.py`、`test_upload_hardening.py`、`test_upload_processing.py`、`test_upload_processing_e2e.py`、`test_youtube_uploader.py`、`test_fake_youtube.py`、`test_upload_worker.py` |
| 秘密・URL | `test_url_guard.py`、`test_codex_adapter.py`、`test_youtube_oauth.py` |
| 契約・境界 | `tests/contract/test_log_contract_vocabulary.py`、`test_compose_workers.py`、`test_dependency_single_source.py`、`tests/architecture/test_layering.py`、`test_no_live_calls.py`、`test_research_workflow_determinism.py`、`test_integration_temporal_client.py`、`test_worker_temporal_connect.py`、`test_docs_contract.py` |

### integration（隔離スタックだけで回す）

```bash
deploy/logging/test/run-e2e.sh run      # 既定: tests/integration 全部（-p apptest_logging_plugin -s）
```

e2e・課金・resume・投稿の中心: `test_incident_recovery_e2e.py`、`test_input_fetch_retry_e2e.py`、
`test_scene_rejection_recovery_e2e.py`、`test_production_e2e.py`、`test_production_rerun.py`、
`test_episode_resume.py`、`test_upload_workflow_persistence.py`、`test_worker_restart_durability.py`、
`test_daily_slot_concurrency.py`、`test_render_workflow_persistence.py`。

## 4. 隔離 app スタックの実績（フェーズ1）

| 日時 (UTC) | コード | 結果 | 所要 | 備考 |
|---|---|---|---|---|
| 2026-09-30 | `01eb0ee`（A・B 実装前） | 108 passed, 1 skipped, 29 errors | 400s | errors は time-skipping test server の取得失敗（network が internal）。イメージに焼いて解消 |
| 2026-09-30 | `a2abccd` | **137 passed, 1 skipped, 0 failed** | 427s（pytest）/ 432s（run 全体）。`up` は 14s、`build` は約2分（キャッシュあり） | ログ実装前の基準値。フェーズ2はこの件数と比べる |
| 2026-10-05 | `2fb696b` | `test_upload_workflow_persistence.py` 4 passed（`-p apptest_logging_plugin`） | 7s | plugin は A 未実装なら no-op |

Temporal Core の WARN（`activity_failure_include_heartbeat`、auto-setup:latest が古い）が stderr に
非 JSON で出る。本番も同じイメージなので、infra 系統（unstructured）に入る行の例として扱う。

skip: `test_pipeline_schedule.py` は `TEMPORAL_ADDRESS = "localhost:7233"` をハードコードしており
隔離スタックの Temporal を見ない（env を読まない）。ホストで走らせると本番スタックの公開ポートへ
繋がる点も含め、別課題として記録（直さない。AGENTS §3）。
