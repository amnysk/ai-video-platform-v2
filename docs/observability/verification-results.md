# ログ基盤（ADR-0040）の検証結果

[verification-plan.md](./verification-plan.md) の必須試験 S-E2E〜S-RB を、隔離環境で実行した結果。
**本番（compose project `avp2`・`avp2-logging`）では何も実行していない。** 有料 provider・YouTube は
fake のみ（隔離 app スタックの network は `internal: true`）。

> この文書は**中間版**。A・B が I-11〜I-17 を修正中で、最終統合版で**全項目を再実行**する（§4）。
> 「前任の暫定値」は 6e1e720（修正前）時点の実測で、出所のファイルを明記して転記した。
> 出所に無い数値は「記録なし」と書き、推測で埋めていない。

## 0. 環境

| 項目 | 値 |
|---|---|
| ホスト | 1台（`yoshiki-XPS-8930`）。rootless Docker。MemAvailable 約 5.2〜5.8GiB、swap はホスト全体でほぼ満杯 |
| 隔離 app スタック | project `avp2-oslog-c`（postgres / minio / temporal。試験ごとに runner / tool コンテナ） |
| 隔離ログ基盤 | project `avp2-oslog-c-log`（opensearch heap 512m・mem_limit 1400m、fluent-bit。Dashboards は起動していない） |
| 資格情報 | `~/.config/avp-logging-test/c/`（0700、各 0600）。env `test`、index `avp-*-test-*` |
| Fluent Bit buffer | tmpfs `/fb-buffer`（64m）、`storage.total_limit_size` 48M（`compose.test.yaml`） |
| 共通の準備 | `source deploy/logging/test/lib.sh`（`sa` = search-assert.py、`sa_admin` = admin 証明書で GET） |

今回（2026-10-06 05:07〜05:45 UTC）の対象 commit は **4110779**（= 6e1e720 に統合 branch d9f27c0 を
merge）と、その上の試験スクリプト修正 569c2e6・1afa4a3。Fluent Bit 設定・Lua・OpenSearch の template /
ISM・`bootstrap.py`・`compose.test.yaml` は 6e1e720 から変わっていない（`git diff --stat 6e1e720 HEAD --
deploy/logging/fluent-bit deploy/logging/opensearch deploy/logging/scripts/bootstrap.py` が空）。
稼働中の Fluent Bit はこの worktree の `deploy/logging/fluent-bit` を mount して 2026-10-05T23:37:20Z に
起動したもの。

前任の証拠の置き場（このセッションの scratchpad。原本は別セッションの scratchpad で、触っていない）:
`…/scratchpad/prev-c/`（faults.json・measure*.csv・ismwatch.log・run-text.log・run-break.log・snap-*.json）、
`…/scratchpad/prev-c-run/run-each-20261005T233740Z.tsv`。片付けた exited コンテナ 42 個の `docker logs -t` と
inspect の要約は `/run/user/1000/avp2-oslog-apptest/avp2-oslog-c/container-logs-20261006T0512Z.tar.gz`
（同じものを今回の scratchpad `c2/container-logs/` にも置いた）。

## 1. 合否一覧

| ID | 今回（4110779） | 前任の暫定値（6e1e720 ほか） | 最終版で再実行 |
|---|---|---|---|
| S-E2E | 未実施（前任データの検索のみ） | 統合テスト 137 passed / 2 skipped / 0 failed。検索で契約フィールド充足・重複 0 | 要 |
| S-CTX | 未実施 | 記録なし | 要 |
| S-REPLAY | 未実施 | `test_worker_restart_durability.py` 3 passed（ログ件数での判定は記録なし） | 要 |
| S-403 | 未実施 | `provider.auth_incident.recorded` 4 件・`provider.call.suppressed` 1 件が索引にある（DB との突き合わせは記録なし） | 要 |
| S-422 | 未実施 | `scene.rejected` 8・`scene.input_refetch` 7・`scene.alternative.started/result` 各7 が索引にある（DB 突き合わせは記録なし） | 要 |
| S-REUSE | 未実施 | `artifact.reused` 52 件が索引にある（シーン数との突き合わせは記録なし） | 要 |
| S-RESUME | 未実施 | json / text / 故障注入の3条件で 137 passed（下記 §2.1）。件数は一致 | 要 |
| S-SEC | 未実施 | 記録なし | 要 |
| S-STOP | 未実施 | 記録なし | 要 |
| S-ROT | 未実施 | 記録なし（今回の S-CAP 1回目で、全速出力時に rotation が Collector を追い越して欠損する様子は観測。§2.9） | 要 |
| S-DUP | 未実施 | 記録なし（S-BAD・S-CAP の範囲では `dupes` 0） | 要 |
| S-CAP | **合格（条件付き）** | 記録なし | 要 |
| S-BAD | **一部不合格**（新規 V-1。不正 JSON の行き先は V-2） | 長大行の stall（I-15） | 要 |
| S-IDX | **合格** | 合格相当（ismwatch.log・snap-a/b が同一） | 要 |
| S-VER | **合格**（Dashboards は image 内の版のみ） | 記録なし | 要（Dashboards の `/api/status`） |
| S-RES | **待機時・障害注入時を記録**（合否は閾値判断の材料） | e2e 実行中を記録 | 要 |
| S-RB | 未実施 | 第2段（text）相当のみ: 137 passed（§2.1） | 要 |

「未実施」は、A・B の修正（I-11〜I-17）で結果が変わり得るため最終版に回したもの、または巨大行を含む
（I-15 の stall を起こす）もの。

## 2. 項目ごとの結果

### 2.1 S-E2E / S-RESUME / S-RB（前任の暫定値のみ）

前任が 6e1e720 系で回した統合テスト（隔離 runner、1回に1つ）:

| 条件 | 出所 | 結果 | 所要 |
|---|---|---|---|
| JSON ログ（`run-e2e.sh run-each`: ファイルごとに別 runner、`-p tests.support.json_log_plugin`） | `prev-c-run/run-each-20261005T233740Z.tsv`、`prev-c/run-each.log` | 27 ファイル: **137 passed, 2 skipped, 0 failed** | 580s（run-each 全体） |
| `APPTEST_LOG_FORMAT=text`（runner-text。出力が stdlib の text 形式であることをログで確認） | `prev-c/run-text.log` 末尾、container-logs の `avp2-oslog-c-runner-text.*` | **137 passed, 2 skipped** | 412.22s |
| ロガー故障注入（runner-break、`APPTEST_BREAK_LOGGING`。ログに `InjectedLoggingFault`） | `prev-c/run-break.log` 末尾 | **137 passed, 2 skipped**。発火回数の記録は runner-break に対しては無い（`prev-c/faults.json` は 23:18Z 作成で runner-break（23:29〜23:36Z）より前の故障注入のもの: `call_observation` 54・`formatter.build` 275・`ledger.after_commit` 216・`ledger.defer` 282・`make_record.INFO` 276・`make_record.WARNING` 17・`reservation_fields` 518） | 416.42s |

- skip 2件: `test_pipeline_schedule.py`（`localhost:7233` 固定、既知。plan §4）と
  `test_logging_collector_lua.py`（runner に docker が無い）。フェーズ1 の基準 a2abccd は 137 passed /
  1 skipped で、差は後者の skip（ログ実装で増えたテスト）。
- runner-break の索引上の `git_sha` は b555dd6、run-each は 6e1e720（下の集計）。text・故障注入の
  2条件は b555dd6 時点の可能性があり、6e1e720 での3条件比較とは言い切れない。

前任の run が索引に残したもの（今回 2026-10-06T05:07Z に `avp-app-test-000001` を検索。この index は
本番 policy で残してある）:

```bash
sa agg --by service_name,git_sha,environment --table
sa agg --by event_name --table
sa dupes
sa fields --require-contract
```

- service_name × git_sha: `test-runner`/6e1e720 3592 件、`test-runner`/b555dd6 694 件、`loggen`/unknown
  609 件、フィールド無し 6 件（全 4901 件）。
- event_name 上位: `log.record` 2079、`activity.succeeded` 909、`artifact.stored` 398、
  `provider.job.state_changed` 276、`reservation.dispatched` 183、`reservation.reserved` 180、
  `reservation.spent` 175、`reservation.job_ref_recorded` 150、`provider.call.succeeded` 140、
  `activity.failed` 72、`stage.started` 68、`artifact.reused` 52、`stage.succeeded` 46、`stage.blocked` 32、
  `render.validation.passed` 20、`stage.skipped` 19、`artifact.superseded` 16、`upload.started` 10、
  `api.request.completed` 10、`scene.rejected` 8、`upload.succeeded` 7、`scene.input_refetch` 7、
  `scene.alternative.started` 7、`scene.alternative.result` 7、`episode.resume.started` 6、
  `provider.auth_incident.recorded` 4、`episode.resume.requested` 4、`reservation.blocked` 3、
  `upload.failed` 2、`reservation.resumed` 2、`upload.skipped` 1、`provider.call.suppressed` 1、
  `episode.resume.rejected` 1（34 種）。
- `dupes`: `duplicated_event_ids` 0。
- `fields --require-contract`（index 全体）: **不合格**。6 件に契約フィールドが無い。6 件とも
  `log_source=unstructured`・`collector_errors=[json_parse_failed]` で、`{` で始まるが JSON として壊れた行
  （loggen の `truncated_json` 5 件、text 形式 runner の `        {` 1 件）。→ V-2。
- 1 Episode での確認（`test_scene_rejection_recovery_e2e.py` の episode `ebe9edbd-…`）:
  `sa fields --term episode_id=ebe9edbd-ad3b-44f0-82ff-5ccd095e447d --require-contract` → 192 件すべて充足、
  `dupes` 0。event_name: `activity.succeeded` 43、`provider.job.state_changed` 28、`artifact.stored` 22、
  `reservation.{reserved,dispatched,spent}` 各16、`reservation.job_ref_recorded` 15、
  `provider.call.succeeded` 14、`stage.{started,succeeded}` 各3、`render.validation.passed` 2、
  `upload.{started,succeeded}` 各1、`scene.rejected`・`scene.alternative.{started,result}` 各1 ほか。
  plan の `provider.call.*`・`reservation.*`・`artifact.stored`・`render.validation.*`・`upload.*`・`stage.*`
  は揃う。`provider_request_id`・`reservation_id` が取得後の行だけにあるかは未確認。
- `upload.succeeded` が 2 件の Episode が1つある（`903113cf-…`、`test_upload_workflow_persistence.py::
  test_concurrent_double_start_and_concurrent_activities_make_one_video`）。同じ video・同じ job で 5ms 差、
  どちらも `reconciled_by=upload_response`。テストは並行2試行で動画1本（`videos_created`）を assert して
  pass。plan の `--expect 1` はこのテストには当てはまらない → V-3。

### 2.2 S-VER（今回: 合格。Dashboards は部分）

2026-10-06T05:07:37Z、4110779。

```bash
sa version --expect 3.8.0
docker inspect -f '{{.Config.Image}} {{.Image}}' avp2-oslog-c-log-opensearch-1 avp2-oslog-c-log-fluent-bit-1
docker exec avp2-oslog-c-log-fluent-bit-1 /fluent-bit/bin/fluent-bit --version
docker run --rm --network none --entrypoint sh <osd image@digest> -c 'grep -m1 "\"version\"" /usr/share/opensearch-dashboards/package.json'
```

| コンポーネント | 稼働版 | image（index digest） | VERSIONS.md |
|---|---|---|---|
| OpenSearch | `{"version": "3.8.0", "distribution": "opensearch"}` | `sha256:fafe3fc3…236a40` | 一致 |
| Fluent Bit | `Fluent Bit v5.1.2`、Git commit `66910c10a4d7…` | `sha256:d792375c…fe9226` | 一致（commit も一致） |
| Dashboards | image 内 package.json `"version": "3.8.0"` | `sha256:7fb7ec1b…852bbd` | 一致 |

- 未検証: Dashboards の `/api/status`（起動していない。メモリ節約のため）。再現:
  `ldc --profile dashboards-setup run --rm dashboards-keystore; ldc --profile dashboards up -d dashboards` の後
  `curl` で `/api/status`。最終版で行う。
- linux/amd64 manifest digest の照合（`docker buildx imagetools inspect`）は外部 registry への問い合わせが
  要るので未実施。

### 2.3 S-IDX（今回: 合格）

2026-10-06T05:09:12Z〜05:24:22Z、4110779。

1. **bootstrap 再実行の冪等性**: `sa_admin snapshot --out a.json; ldc run --rm -e AVP_ISM_POLICY_DIR=ism
   bootstrap; sa_admin snapshot --out b.json; diff a.json b.json` → **diff 空**。bootstrap の出力は
   `ISM policy avp-app unchanged`・`ISM policy avp-infra unchanged`・`write alias avp-app-test-write ->
   avp-app-test-000001`。
2. **alias 名・typo index の自動作成拒否**:
   - admin で `PUT avp-app-ztest-write/_doc/c2probe` → **404** `index_not_found_exception`（`[action.auto_create_index]
     contains [-avp-*] which forbids automatic creation`）。`avp-app-test-000099` も同じ 404。
   - `avp_fluentbit` で `PUT avp-app-ztest-write/_doc/x` → 404（同上）、`PUT /zz-c2probe/_doc/x` → **403**
     `no permissions for [indices:data/write/index]`、`PUT /avp-app-ztest` → **403** `indices:admin/create`。
   - 直後の snapshot で index は増えていない（`avp-app-test-000001`・`avp-infra-test-000001` のみ）。
3. **短縮 ISM・rollover・delete**: `ldc run --rm bootstrap`（`ism-test`: 2件または 2分で rollover、rollover 後
   3分で削除、`job_interval` 1分）→ 出力 `ISM policy avp-app updated（既存の管理対象 index は旧版のまま）`。
   前任の run が残した `-000001` を消さないため、admin で `POST avp-{app,infra}-test-write/_rollover` を1回だけ
   手で行い、`-000002` 以降を短縮 policy で作らせた。以後は loggen（`--count 5` を 50 秒おきに3回）と
   Temporal の infra ログで ISM 任せ。監視ログ: scratchpad `c2/ismwatch.log`（30 秒間隔）。
   - 05:09:50 write index `-000002`（手動）→ **05:12:21 ISM が `-000003` へ rollover**（app・infra とも）。
   - **05:16:58 までに `avp-app-test-000002` が ISM の delete で消えた**。failed 0（全 index で `failed: null`）。
4. **本番 policy への復帰**: `ldc run --rm -e AVP_ISM_POLICY_DIR=ism bootstrap`（05:17:07）。短縮 policy で
   作られた `-000003`〜`-000005` は短縮版のまま rollover・削除され、05:24:22 に
   `avp-{app,infra}-test-000001`（本番 policy）と `-000006`（write）だけになった。`_plugins/_ism/explain` で
   `-000006` は `policy_seq_no` 1644 = 現行の本番 policy（`min_index_age 7d / min_primary_shard_size 5gb`）、
   手動 rollover した `-000001` は `transition` に進み failed なし。
   - `snapshot` の diff は index・alias 以外に ISM の `ism_template.last_updated_time` だけが出た（内容は同じ）。
     試験スクリプトの比較漏れとして 1afa4a3 で修正し、修正後の snapshot では `ism_policies` も一致。

前任の暫定値（6e1e720、`prev-c/ismwatch.log`・`prev-c/snap-*.json`）: 23:17:27〜23:23:29 に
`avp-app-test-000001` → `-000004` まで rollover、23:23:29 に `-000001` が消えた（failed 無し）。
`snap-a.json` と `snap-b.json` は同一（bootstrap 再実行の diff 空）。`snap-prod.json` は本番 policy の
`min_index_age 7d` 等に戻ったこと。

### 2.4 S-BAD（今回: 一部不合格）

2026-10-06T05:24:34Z〜05:28:50Z、4110779。長大行は 40000 bytes（Docker が 16KiB で partial に分け、結合後
256k 未満。I-15 の stall を起こさない大きさ）。

```bash
deploy/logging/test/fault-bad-lines.sh 500 40000          # tag bad-052434-29798
```

- 正常行: `sa count --term request_id=bad-052434-29798 --term event_name=log.record --min 500` → **537**
  （= 正常 500 + JSON として読める壊れ行 36 + 長大行 1。長大行は skip されず取り込まれた）。`dupes` 0、
  537 件すべて契約フィールドあり。
- 型不整合の退避: `sa collector-errors` → 32 件。`@timestamp_replaced` 8、`duration_ms`・`episode_id`・
  `http_status`・`provider_body`・`retryable`・`scene_revision`・`totally_unknown_field` 各4。
  代表: `{"collector_errors": ["episode_id"], "log_source": "app_json", "attributes": {"collector_moved":
  {"episode_id": {"nested": "x"}}, "seq": -1}}`。
- 壊れた行の行き先: JSON でない行（`not_json` 5）と配列（`json_array` 4）は **infra**（`avp-infra-test-*`、
  `log_source=unstructured`、stdout 9 件）。`{` で始まり壊れた行（`truncated_json` 5）は **app index** に
  `log_source=unstructured`・`json_parse_failed` で入る（契約フィールド無し）。→ V-2。
- 後続行: `bad-after-…` 50 件が 3 秒で届いた。Fluent Bit の `retries`・`dropped`・`long_line_skipped` は 0 のまま。

**Bulk 部分失敗の追加試験**（plan の「Bulk 部分失敗」は上の loggen では起きない: 型不整合は Lua が先に直すので
OpenSearch は 1 件も拒否しなかった）。正常 20 行・試験行 1・正常 20 行を1コンテナずつ出す
（scratchpad `c2/bulkprobe.py`。`run-e2e.sh tool python -c "$(cat bulkprobe.py)" <tag> <kind>`）:

| kind | 試験行 | 届いた件数 / 41 | 結果 |
|---|---|---|---|
| `int_float` | `scene_revision: 1.5` | 41 | そのまま格納 |
| `bool_number` | `retryable: 1` | 41 | `collector_moved.retryable` へ退避 |
| `attributes_scalar` | `attributes: "scalar"` | 41 | そのまま格納（`enabled:false`） |
| `int_overflow` | `http_status: 99999999999999999999` | 41 | `1e+20` で格納（`ignore_malformed`） |
| `num_overflow` | `duration_ms: 1e400`（JSON の数値） | 40 + unstructured 1 | JSON 解釈失敗で app index に unstructured（V-2 と同じ経路） |
| `str_inf` | `duration_ms: "inf"` | **0** | chunk ごと再送（下記） |
| `str_nan` | `duration_ms: "nan"` | **0** | 同上 |

`str_inf`・`str_nan` は、試験行だけでなく**同じ chunk の正常 40 行も届かない**。Fluent Bit は
`failed to flush chunk '1-1791264440.38473860.flb', retry in …`（`str_nan` は `1-1791264490.…`）を繰り返し、
`fluentbit_output_retried_records_total{name="avp_app"}` が 0 → 369、`retries_total` 0 → 9。
`retries_failed`・`errors_total` は 0 のまま（失敗が metrics の破棄・エラーに出ない）。2つの chunk は後の
S-CAP で `evicted from output queue to make room under storage.total_limit_size` として破棄された
（05:40:08）。→ V-1。

前任の暫定値（6e1e720 系、container-logs と今回の検索）: `fault-bad-lines.sh`（tag `bad-232506-22675`）で
app_json 537・unstructured（app index）5、`bad-after-232518-10920` 50 件。長大行: `long10000`・`long20000`・
`long40000` は各 4/4 件届き、`long300000-19966`（3 行 + 300000 bytes）は **0 件**（同じコンテナの正常 3 行も
届かない）、`rep300k-25927` も 0 件。stall の時刻・CPU の記録は review-log I-15 の記述以外に無い。

### 2.5 S-CAP（今回: 合格（条件付き））

2026-10-06T05:38:27Z〜05:44:52Z、569c2e6。buffer は tmpfs 64m（`storage.total_limit_size` 48M）。

```bash
deploy/logging/test/fault-capacity.sh 200000 400 2000     # tag cap-053827-25090
```

（実行した版は `catchup` の非0終了（未読あり）で止まったため、OpenSearch 停止中のまま `catchup` 待ち以降を同じ手順で
手で実行した。scratchpad `c2/cap-run2.log`・`c2/cap-run2-manual.log`。`catchup || true` に直した版を 569c2e6 として commit）

- ホストのディスク: 開始前・停止中とも `/dev/nvme0n1p2 915G 113G 757G 13%`（変化なし）。
- アプリ側: loggen は OpenSearch 停止中に 200000 行を 123 秒で出し終えた（rate 指定 2000/s に対し実測
  約 1600/s。停止で詰まっていない）。
- 停止中: buffer `du` 44.0M、`total_chunks` 86、`fluentbit_output_dropped_records_total{name="avp_app"}`
  **0 → 77057**。Fluent Bit のログに `chunk … evicted from output queue to make room under
  storage.total_limit_size … limit=48000000`。
- 復旧後: 起動直後は `OpenSearch Security not initialized`（503）で再送し、`succeeded at retry 4〜6` で解消。
  件数は 05:44:22 に 113002 で止まった。`seq=199999`（最新）・`seq=150000` は届き、`seq=0〜100000` は無い
  （古い側が破棄）。`dupes` 0、`retries_failed` 0。
- 条件: 200000 − 113002 − 77057 = **9941 件の行方が metrics に出ない**。77057 には V-1 の試験で詰まっていた
  82 件も含まれるので、実際は 10023 件。loggen 終了時に Collector は最古の rotation ファイル（`.log.4`）を
  まだ読み切っておらず（`catchup` で 43MB 遅れ）、出力総量（約 137MB）が json-file の保持量（20m × 5）を
  超えていたので、rotation で読む前に消えた分と推定する（証明はしていない）。

1回目（05:29:50、修正前のスクリプト、全速）は不成立: 200000 行が数秒で出て rotation が一巡し、
118114 行が読まれる前に消えた（`docker logs` の先頭が `seq=118114`）。buffer は 22.1M で上限に届かず
`dropped` 0、届いたのは 98737 件（= 消える前に読めた 16851 + 残った 81886）。また OpenSearch の起動待ちを
せずに数えて TLS EOF で失敗した。どちらも試験スクリプトの不具合として 569c2e6 で直した。

### 2.6 S-RES（今回: 待機時・障害注入時を記録）

`deploy/logging/test/measure.sh <csv> <回数> 10`。CSV は scratchpad `c2/measure-idle.csv`・`c2/measure-cap.csv`。

| 条件 | 期間（UTC） | OpenSearch mem | OpenSearch CPU 最大 | Fluent Bit mem | Fluent Bit CPU 最大 | MemAvailable |
|---|---|---|---|---|---|---|
| 待機（今回） | 05:07:50〜05:08:50（6回） | 1079MiB（一定） | 1.6% | 19MiB | 1.2% | 5.23〜5.54GiB |
| S-BAD・S-CAP 中（今回） | 05:29:45〜05:41:39（60回） | 670〜1076MiB（停止・再起動を含む） | 408%（再起動・追いつき時） | 17〜69MiB（tmpfs の buffer を含む） | 103.8%（追いつき読み取り時） | 4.44〜6.63GiB |
| e2e 実行中（前任、`prev-c/measure-load.csv`） | 2026-10-05 23:37:44〜23:48:26（30回） | 最大 1099MiB | 8.4% | 最大 11MiB | 4.7% | 4.86〜5.63GiB |
| text 実行中（前任、`prev-c/measure.csv`） | 23:22:22（1回） | 1.002GiB | 5.84% | 8.3MiB | 0.49% | 5.53GiB（5795256 KiB） |

- OpenSearch: `VmRSS` 1096092 kB、`VmSwap` 0 kB、cgroup `memory.swap.current` 0。`mem_limit` = `memswap_limit`
  = 1468006400（swap を使えない）。`OPENSEARCH_JAVA_OPTS=-Xms512m -Xmx512m`。RSS ≤ mem_limit。
- ホストの SwapFree は 0〜5468 KiB（ホスト全体。隔離スタックの外の要因も含む）。
- ADR §7 の閾値（2GiB / 1.5GiB）との比較: MemAvailable の最小は 4.44GiB（OpenSearch 停止中を含む区間）。
  判断材料として記録するだけで、合否は付けない。

### 2.7 未実施の項目の再現コマンド

最終版（A・B の修正を merge した branch）で、§0 の準備の後に:

```bash
T=deploy/logging/test
$T/run-e2e.sh build && $T/run-e2e.sh up
APPTEST_ISM=prod $T/logging-stack.sh up                   # e2e の証拠が消えないよう本番 policy
$T/run-e2e.sh run-each                                     # S-E2E・S-CTX・S-REPLAY・S-403・S-422・S-REUSE（JSON）
APPTEST_LOG_FORMAT=text $T/run-e2e.sh run                  # S-RESUME / S-RB 第2段
APPTEST_BREAK_LOGGING=1 $T/run-e2e.sh run                   # S-RESUME（ロガー故障注入）
$T/fault-secrets.sh                                        # S-SEC
$T/fault-opensearch-stop.sh 120 3000                       # S-STOP
$T/fault-collector-rotation.sh 20000 200                   # S-ROT
$T/run-e2e.sh tool python $T/loggen.py --tag d1 --count 1000 --duplicate-every 10   # S-DUP
$T/fault-bad-lines.sh 500 40000; $T/fault-bad-lines.sh 500 300000                   # S-BAD（300000 は I-15 修正後）
$T/fault-capacity.sh 200000 400 2000                       # S-CAP
$T/measure.sh "$XDG_RUNTIME_DIR/m.csv" 30 10               # S-RES（各試験と並行）
```

## 3. 今回見つかった不具合・食い違い

ID は暫定（V-n）。review-log への登録（I 番号の付与）は統合 branch 側で行う。

| ID | 重大度 | 担当 | 内容 | 根拠 |
|---|---|---|---|---|
| V-1 | Medium | B | 数値フィールドに文字列 `"inf"` / `"nan"`（Lua の `tonumber` が受け付ける）が来ると、Lua は数値として通し、Bulk 全体が失敗して **chunk ごと**再送される。同じ chunk の無関係な正常行も届かず、retry 上限（72 回、約6時間）か容量上限で破棄される。`errors_total`・`retries_failed_total` には出ず `retried_records_total` だけが増える。`1e999` のような文字列も同じ経路の可能性（未試験）。型修復で非有限値（`v ~= v`・`±math.huge`）を退避すれば防げる見込み | §2.4 の `str_inf`・`str_nan`（0/41） |
| V-2 | Low | B（または plan を C が直す） | `{` で始まり JSON として壊れた行は app index に `log_source=unstructured`・`json_parse_failed` で入り、`REQUIRED_APP_FIELDS` を持たない。plan の S-BAD は「不正 JSON は infra 系統」、S-E2E は「全文書が `REQUIRED_APP_FIELDS` を持つ」。どちらに合わせるか決める | §2.1 の 6 件、§2.4 の `truncated_json` 5 件・`num_overflow` 1 件 |
| V-3 | Low | C（plan）・A（確認） | 並行2試行の upload テストで、動画1本に対し `upload.succeeded` が2件（どちらも `reconciled_by=upload_response`）。plan の「`upload.succeeded` は1回」は並行試験に当てはまらない。2件目が `upload_response` と書かれるのが正しいかは A が確認 | §2.1 の episode `903113cf-…` |
| V-4 | Low | C（修正済み） | `fault-capacity.sh` が rotation の欠損を測っていた・起動待ち無し。`snapshot` の比較漏れ | 569c2e6・1afa4a3 |

観測（不具合ではない）: 全速出力（約 137MB / 2 分、または 112MB / 数秒）では json-file の rotation
（20m × 5）が Collector の読み取りを追い越し、消えた行は metrics にも `catchup`（残っているファイルしか
見ない）にも出ない。plan の S-ROT「一巡させる量: 欠損が出る」の実例。

## 4. 最終版で再実行すべき項目

全項目（S-E2E〜S-RB）。特に:

- **S-BAD** の長大行（300000 bytes。I-15 の修正確認）と V-1 の `str_inf`・`str_nan`。
- **S-E2E / S-CTX / S-REPLAY / S-403 / S-422 / S-REUSE / S-RESUME**: A の修正（I-1〜I-4・I-8・I-10 は
  d9f27c0 に入った。I-11・I-12 は未修正）後の image で、3条件の件数比較を同じ commit でやり直す。
- **S-SEC / S-STOP / S-ROT / S-DUP / S-RB**: 未実施。
- **S-VER**: Dashboards の `/api/status`。
- **S-IDX**: 今回の手動 rollover を使わず、新しい stack（`logging-stack.sh up`）で plan どおりに。
- I-17（check-pipeline の追いつき確認）の修正後、runbook §7 の手順を `catchup.py` から置き換えて確認。
