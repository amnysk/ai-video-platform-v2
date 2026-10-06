# ログ基盤（ADR-0040）の検証結果

[verification-plan.md](./verification-plan.md) の必須試験 S-E2E〜S-RB を、隔離環境で実行した結果。
**本番（compose project `avp2`・`avp2-logging`）では何も実行していない。** 有料 provider・YouTube は
fake のみ（隔離 app スタックの network は `internal: true`）。

> §0〜§4 は中間版（4110779、修正前の B 設定）。**最終版（統合 94d52f4、I-11〜I-20 修正済み）の結果は §5**。
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

## 5. 最終版（統合 94d52f4）の結果

verify branch で 94d52f4 を merge した commit は 990d7e1、試験の実行コードは **517c9ca**（990d7e1 + 試験
スクリプトの project 名の引数化）。§5 の記録の commit（docs だけ）は試験の後に積むので、それ以降の
`git_sha` は記録 commit を指すことがある（コードは 517c9ca から変えていない）。

### 5.0 環境（新規に作り直した隔離スタック）

| 項目 | 値 |
|---|---|
| app | project `avp2-oslog-c2`（`run-e2e.sh build` で image `avp2-oslog-c2-{worker,runner}:test` を 517c9ca から作り直し、`run-e2e.sh up`） |
| ログ基盤 | project `avp2-oslog-c2-log`（`APPTEST_ISM=prod logging-stack.sh up`。最終版の `fluent-bit.yaml`・Lua を mount、port 19213） |
| 秘密 | `~/.config/avp-logging-test/c2/`（`init-secrets.sh --env test` で**新規生成**。本番 `~/.config/avp-logging/prod` には触れていない） |
| 旧スタック | `avp2-oslog-c`・`avp2-oslog-c-log` は停止（volume は残す）。OpenSearch を2つ同時に動かしていない |
| env | scratchpad `c2/c2.env`（`AVP_APPTEST_PROJECT=avp2-oslog-c2 LOGGING_PROJECT=avp2-oslog-c2-log AVP_LOGGING_SECRETS_DIR=~/.config/avp-logging-test/c2 AVP_LOGGING_OS_PORT=19213 OPENSEARCH_X509_STRICT=1`） |
| 証拠 | scratchpad `c2/final/`（run-each・text・break のログ、measure*.csv、検索結果） |

**I-16（CA の keyUsage）: 合格。** 新しい CA は `X509v3 Key Usage: critical  Certificate Sign, CRL Sign`、
`Basic Constraints: critical CA:TRUE`（旧 CA は Key Usage 無し）。`OPENSEARCH_X509_STRICT=1`（Python 3.13.14 の
`VERIFY_X509_STRICT` を外さない）で `sa version` → `{"version": "3.8.0"}`、admin 証明書での alias 読み取りも成功。
§5 の検索はすべて strict のまま行った。

### 5.1 S-E2E（合格）

既存 integration を3条件・同一コード（517c9ca）で実行:

| 条件 | 実行 | 結果 | 所要 |
|---|---|---|---|
| JSON ログ | `run-e2e.sh run-each`（27 ファイル、ファイルごとに runner `avp2-oslog-c2-r-<stem>`）06:13:31〜06:22:38Z | **137 passed, 6 skipped, 0 failed** | 547s |
| text | `APPTEST_LOG_FORMAT=text … run pytest tests/integration -p tests.support.json_log_plugin`（runner `avp2-oslog-c2-runner-text2`）06:23:30〜06:30:22Z | **137 passed, 6 skipped** | 407.10s |
| ロガー故障注入 | `APPTEST_BREAK_LOGGING=1 …`（runner `avp2-oslog-c2-runner-break2`）06:30:22〜06:37:41Z | **137 passed, 6 skipped** | 433.79s |

- skip 6 は3条件で同じ: `test_logging_collector_lua.py` 5（runner に docker が無い）、`test_pipeline_schedule.py` 1（既知）。
- 故障注入の発火: runner のログに `InjectedLoggingFault` が 776 行（`AVP_TEST_FAULT_REPORT` で発火回数を
  ファイルに出す指定をしたが、コンテナ内に report が残らず回収できなかった。回数の内訳は記録なし）。
- 最初の text 実行（`avp2-oslog-c2-runner-text`）は report 指定のやり直しのため 16 秒で止めた（exit 143）。結果に使っていない。

OpenSearch での検索（strict TLS、viewer）:

```bash
sa agg --by service_name,git_sha,environment --table   # test-runner 517c9ca… test 3591（JSON 実行の直後）
sa fields --require-contract                           # docs 3591 すべて契約フィールドあり（I-19 の修正後、欠けた文書 0）
sa dupes                                               # duplicated_event_ids 0
```

1 Episode（`test_scene_rejection_recovery_e2e.py`、episode `89b1a8c3-de4c-4e4d-b639-ab05bc086de2`）:

```bash
sa count  --term episode_id=89b1a8c3-de4c-4e4d-b639-ab05bc086de2                       # 192
sa fields --term episode_id=89b1a8c3-… --require-contract                              # 192 件すべて
sa dupes  --term episode_id=89b1a8c3-…                                                 # 0
sa count  --term episode_id=89b1a8c3-… --term event_name=upload.succeeded --expect 1   # 1
sa agg    --term episode_id=89b1a8c3-… --by event_name --table
```

| event_name | 件数 | `reservation_id` あり | `scene_id` あり |
|---|---|---|---|
| activity.succeeded | 43 | 13 | 31 |
| provider.job.state_changed | 28 | 28 | 28 |
| artifact.stored | 22 | 13 | 17 |
| reservation.reserved / dispatched / spent | 各16 | 16 | 15 |
| reservation.job_ref_recorded | 15 | 15 | 14 |
| provider.call.succeeded | 14 | 14 | 14 |
| log.record | 6 | 0 | 4 |
| stage.started / stage.succeeded | 各3 | 0 | 0 |
| render.validation.passed | 2 | 0 | 0 |
| upload.started / upload.succeeded | 各1 | 0 / 1 | 0 |
| scene.rejected / scene.alternative.started / scene.alternative.result | 各1 | 1 / 0 / 0 | 1 |
| stage.skipped・artifact.superseded・activity.failed | 各1 | | |

- `reservation_id` は予約より前の行（`stage.*`・`upload.started`・`scene.alternative.*`）に無く、予約後の行にだけある。
- `provider_request_id` は 0 件: fake provider は `fal_queue.py`（`provider_request_id` を付ける唯一の経路）を
  通らない。実 adapter 側は unit test の担当（emission-points.md）。
- 代表文書（秘密なし。`c2/final/e2e-ep-samples.jsonl`）: `scene.rejected` は `http_status` 422・`error_category`
  `content_policy`・`error_code` `["content_policy_violation"]`・`classification_basis` `provider_error_type`・
  `scene_id` sb6・`reservation_id` あり、`response_excerpt` に `"reason": "partner_validation_failed"`。
  `upload.succeeded` は `attributes.reconciled_by=upload_response`・`video_id=vid00000002`（fake）・`reservation_id` あり。
  `stage.started` は `workflow_id=episode-89b1a8c3-…-production`・`run_id`・`stage=production`。

### 5.2 S-CTX（合格）

```bash
sa agg --term log_source=app_json --by run_id,activity_id,episode_id --table   # → c2/final/ctx.tsv を集計
```

- Activity 実行（`run_id`×`activity_id`）970 件・文書 4289 件のうち、**2つ以上の `episode_id` を持つ実行は 0**。
  episode の付いた文書と付かない文書（temporalio の `Completing activity as failed` 等）が混ざる実行は 66（前の
  episode が残ったのではなく、episode を持たない SDK の行）。
- 並行の upload 試験（`test_concurrent_double_start…`、episode `5f551644-…`）の文書はすべてその episode。
- `test_daily_slot_concurrency.py` は app 系統の文書を出さない（infra に stdout 2 行のみ）。ログでは検証できない。
- A の unit test: `tests/unit/test_log_activity_interceptor.py::test_two_episodes_in_parallel_do_not_mix` ほか
  （`test_log_activity_interceptor.py`・`test_log_activity_events.py`・`test_log_emit.py`・`test_log_workflow_replay.py`・
  `test_log_old_history_replay.py`）をホストで実行し **56 passed**。

### 5.3 S-REPLAY（合格）

- `test_worker_restart_durability.py` 3 passed（3条件とも。worker の SIGKILL と引継ぎを含む）。この試験の worker
  子プロセスは stdout をファイルへ向けるので、索引に載るのは runner 側の 13 件だけ（plan §2-3 のとおり）。
- `sa agg --term event_name=stage.started --by container_name,run_id --table` → workflow run 68 件・文書 68 件、
  **同じ run に `stage.started` が2件以上ある run は 0**。index 全体の `dupes` 0。
- A の replay unit test（`test_log_workflow_replay.py::test_workflow_events_are_emitted_once_and_replay_emits_nothing`、
  `test_log_old_history_replay.py`）は上の 56 passed に含む。

### 5.4 S-403（合格）

container `avp2-oslog-c2-r-incident-recovery-e2e`。DB の行数はテスト自身の assert（スキーマはテストごとに作って
消すので、試験後に DB を直接数えられない）と突き合わせた。

```bash
sa agg --term container_name=avp2-oslog-c2-r-incident-recovery-e2e --term event_name=provider.auth_incident.recorded \
  --by episode_id,scene_id,provider,http_status,error_category,classification_basis --table
sa agg --term container_name=… --term event_name=provider.call.suppressed --by episode_id,scene_id,provider,error_category --table
sa agg --term episode_id=1430e9f5-a954-49ee-8a6b-cde8555fa692 --term event_name=reservation.reserved --by provider,scene_id --table
```

| テスト | DB 側（テストの assert） | OpenSearch |
|---|---|---|
| `test_auth_incident_threshold…`（episode `9922fa22-…`） | `count_unresolved_within_window(FAL_VIDEO) == 3`、sb4 は prepare も呼ばれず止まる | `provider.auth_incident.recorded` **3**（sb1・sb2・sb3、`http_status` 403・`error_category` access_denied・`classification_basis` http_status_only）、`provider.call.suppressed` **1**（sb4、`suppressed_by_incident`） |
| `test_sb6_403_blocks…`（episode `1430e9f5-…`） | sb6 は prepare で 403、復旧後 sb6 だけ新規 submit・他は再課金なし | `provider.auth_incident.recorded` 1（sb6、403・access_denied・http_status_only）。`reservation.reserved` は各 scene・各 provider 1 件（sb6 の動画も1件 = 復旧後の1回）。`artifact.reused` 11（画像 6・動画 5）。`stage.started` は run 2つで各1 |

- 403 は credentials と断定していない（`classification_basis=http_status_only`）。
- `provider.call.failed` は 0 件: fake は `fal_storage.py` / `fal_queue.py` を通らない（テスト自身のコメントのとおり）。
  plan の「`provider.call.failed` に http_status=403」は fake の integration では検証できない → 食い違い V-5（§5.15）。

### 5.5 S-422（合格）

| テスト | DB 側（テストの assert） | OpenSearch |
|---|---|---|
| `test_scene_rejection_recovery_e2e.py`（episode `89b1a8c3-…`） | `provider_rejections` 1 行（sb6、`partner_validation_failed`）、planner 1 回・override revision 1、fal_video 予約は sb6 が 2・他 1、codex 1、youtube 1 | `scene.rejected` **1**（sb6）、`scene.alternative.started/result` 各 **1**（`scene_revision` 1）、`reservation.reserved` は fal_image 7・fal_video 7（sb6 が 2、他 1）・codex_scene_alternative 1・youtube_upload 1 |
| `test_incident_recovery_e2e.py::test_422…`（episode `f6bdfc25-…`） | sb6 の video が 422、版を上げても再課金なし | `scene.rejected` 1（sb6、422・content_policy・provider_error_type） |
| `test_input_fetch_retry_e2e.py`（4件） | — | episode `892f574d-…`: `scene.input_refetch` 2・`scene.rejected` 1・`upload.succeeded` 1。`596385ff-…`: `scene.input_refetch` 2・`scene.rejected` 2・`reservation.blocked` 1・`artifact.reused` 11。`a145dce8-…`: rejected 1・alternative 各1。`dd21d426-…`: rejected 2・alternative 各2 |

- input_fetch 系の DB 行数との1対1の突き合わせ（`scene.input_refetch` と上げ直し回数）は、テストの assert が回数を
  直接持たないため未実施。件数は記録のみ。

### 5.6 S-REUSE（合格）

- `test_production_e2e.py::test_production_end_to_end_then_rerun_reuses_everything`（episode `cd32c2ab-…`、4 scene）:
  1回目の run（`01a10fda-729e-…`）は `reservation.reserved` 8・`provider.call.succeeded` 8、rerun の run
  （`01a10fda-84b4-…`）は `artifact.reused` **8**（sb1〜sb4 × 画像・動画）で submit 0。テストの assert
  （submit 回数が増えない）と一致。
- `artifact.reuse_rejected` は 0 件: emission-points.md で **v1 未発行**（破損は `infrastructure.artifact.verify` の
  `log.record` ERROR `ARTIFACT_VERIFICATION_FAILED … verdict=corrupt_hash`）。`test_corrupt_artifact…`（episode
  `96e5b1a1-…`）でその ERROR 2 件を確認。plan の記述が古い → V-5。

### 5.7 S-RESUME（合格）

- 3条件で既存テストの合否が同じ（§5.1）。
- I-20（並行 upload）: episode `5f551644-…` で `upload.succeeded` **1**・`upload.reused_existing` **1**・
  `reservation.spent` **1**・`reservation.reserved` 1。他の upload 試験の episode は `upload.succeeded` 1 / `spent` 1、
  unknown outcome（`5f747238-…`）は `upload.failed` 2・`spent` 0。
- 未照合予約: `reservation.blocked` は production_e2e（`70d9ccca-…`、ambiguous submit）と input_fetch（`596385ff-…`）で
  各1。その後の再送（同じ scene の `reservation.reserved` 増加）は無い（テストの submit 回数 assert も pass）。
- `episode.resume.*`: 並行 resume（episode `25fca168-…`）で索引上 `requested` 1・`started` 1・`rejected` 1（409）、
  `c69526fb-…` は `requested` 1・`started` 2。json-file には `requested` が各 2 件ある。索引に無い 2 件は、pytest の
  進捗の `.` が JSON 行の先頭に付いた行（`.{"@timestamp":…`）で、Collector は JSON と見なさず infra 系統
  （`log_source=unstructured`）に入れた。run-each の 27 コンテナで 84 行（infra の `message` が `{"@timestamp` で
  始まる文書 84 件と一致）。**試験 harness の問題**（`tests.support.json_log_plugin` が pytest の端末出力と同じ
  stdout に書く）で、本番の worker には pytest の進捗出力が無い → V-6（§5.15）。§5 の件数は、各テストの最初の
  1 行程度がこの理由で app 系統から欠け得る（上の S-403/S-422/S-REUSE の件数はテストの assert と一致した）。

### 5.8 S-SEC（合格。raw の print に残るリスク1種）

2026-10-06T09:56:16Z、`deploy/logging/test/fault-secrets.sh`（8b4ae60 で判定を logging 経由と raw に分けた版。
1回目 09:54:24Z は旧版で、json-file の判定に raw の print が混ざり、infra の全走査が 400 で止まった。§5.15 V-7）。
OpenSearch を止めて buffer に留め、needle（実行ごとの乱数 16 種 + 隔離スタックの DB・MinIO パスワード 2 = 18）を
logging 経由と `--raw`（print、stdout・stderr）の両方で出した。

| 場所 | 結果 |
|---|---|
| json-file（logging 経由の JSON 行） | **0 件**（56 行中。print の行は 30 行に needle、= 14 種 × stdout/stderr + PEM 本文の継続行 2） |
| Fluent Bit buffer（tmpfs）・位置 DB | needle の hit は **PEM（needle 行 5）だけ、1 ファイル**。他の 15 種とパスワードは 0 |
| OpenSearch app（`sa absent --needles-file needles-logging.txt --since …`） | **0 件**（22 文書を全走査、18 needles） |
| OpenSearch infra / 全体（raw needle） | PEM の needle が **2 文書**（print の stdout・stderr）。他の 14 種は 0 |
| `redaction_applied=true` | 52 文書（`--since` 以降、app + infra）。この tool の infra 文書は stdout・stderr 各 15 が true、各 2 が false |

- logging を通る経路（A の整形器 + Collector）は全種を伏せた。
- 残るリスク: logging を通らない print で PEM を出すと、改行で行が分かれ、`BEGIN … KEY` を含まない本文の行は
  Collector が秘密と判定できない（行単位の収集の限界）。plan の「残った種類は ADR の残るリスクとして報告」に当たる。
- needle の値は `/run/user/1000/avp2-oslog-apptest/avp2-oslog-c2/secrets-*`（0600、乱数の偽物）。試験後に削除する。

**最終統合 c5af8ff（Lua の伏せ字規則 I-22/I-23）で再実行**（verify 7f03f7d、Fluent Bit を `ldc up -d --force-recreate
--no-deps fluent-bit` で再作成し、コンテナ内の `avp_collector.lua` の sha256 `5862f256…` = c5af8ff の blob を確認）:

- 10:21:28Z（7f03f7d の script）: json-file の JSON 行 0・buffer は PEM だけ 1 ファイル・app 0（22 文書）。ただし infra の走査が
  34 件中 2 件しか届いていない時点で行われ「無い」と出た（試験スクリプトの不具合。待ってから手で走査し直すと PEM 2 文書）。
- 10:22:45Z（infra の到着を待つ修正後の script）: **json-file（JSON 行）0、buffer は PEM（needle 行 5）1 ファイルのみ、
  app 0（22 文書・18 needles）、infra は 34 文書を待ってから走査し PEM 2 文書（print の stdout・stderr）のみ、raw の他 14 種 0、
  `redaction_applied=true` 52**。94d52f4 と同じ結果（伏せ字規則の変更で後退なし）。

### 5.9 S-STOP（一部不合格: Fluent Bit の restart で 1〜2 行が欠ける）

**OpenSearch 停止**（09:58:49〜10:05:58Z、`fault-opensearch-stop.sh 120 3000`）: 合格。

- loggen（rate 50/s、3000 行）は停止中に 61 秒で完了（業務側は遅れない）。
- 停止中: `retried_records_total{avp_app}` 176 → 13963、`dropped` 0、`retries_failed` 0。
- 復旧後: `sa count --term request_id=osstop-095849-31948 --expect 3000` → **3000**、`dupes` **0**。`retries_failed` 0。
  全件そろうまで再起動から約5分（再送の backoff）。

**Fluent Bit の `docker restart`**（loggen 2000 行・rate 100/s の途中で `docker restart avp2-oslog-c2-log-fluent-bit-1`）:
**3回とも欠損**、重複は 0。

| tag | 届いた件数 / 2000 | 欠けた seq | 重複 |
|---|---|---|---|
| `fbrestart-100607` | 1998 | 719, 720 | 0 |
| `fbrestart2-101141` | 1998 | 717, 718 | 0 |
| `fbrestart3-101616` | 1999 | 711 | 0 |

- 欠けた行は json-file にある（`fbrestart-100607` の json-file は 2001 行 = 2000 + stderr 1）。infra にも無い。
- Fluent Bit のログは `caught signal (SIGTERM)` → `pausing all inputs` → `service has stopped (0 pending tasks)`（5 秒の
  grace 内に正常終了）。`dropped`・`retries_failed` は 0 のまま（metrics に出ない）。
- 推定: tail は読んだ位置を位置 DB に進めたが、その行が rewrite_tag の emitter（`emitter_storage.type: filesystem`）の
  chunk に入る前に停止した。証明はしていない → V-8（§5.15）。
- 計画上の「worker SIGKILL で別 worker が引き継ぐ」は `test_worker_restart_durability.py`（3条件で pass、§5.1・§5.3）。

### 5.10 S-BAD（合格。最終統合 c5af8ff の Lua で実施）

verify 61dc077 以降、Fluent Bit は c5af8ff の Lua（§5.8 で再作成）。2026-10-06T10:24:00〜10:26:00Z。

**`fault-bad-lines.sh 500 40000`**（tag `bad-102400-25006`）:

- 正常行 `count --term request_id=… --term event_name=log.record --min 500` → **537**（正常 500 + JSON として読める壊れ行 36 + 40000 bytes の行 1）。
- 型不整合の退避 `collector-errors` → 32 件（`@timestamp_replaced` 8、`duration_ms`・`episode_id`・`http_status`・`provider_body`・
  `retryable`・`scene_revision`・`totally_unknown_field` 各4）。
- **I-19**: `{` で始まり壊れた行 5 件は **infra**（stdout・`collector_errors=json_parse_failed`）。app 系統のこの container は
  `app_json` 537 のみ。index 全体の `sa fields --require-contract` は 13959 文書すべて契約フィールドあり。
- 後続 `bad-after-…` 50/50。`long_line_skipped` 0。

**I-15（300000 bytes の行）**: 同じファイルに「50 行 → 300000 bytes の英数字の1行 → 50 行」（scratchpad `c2/longprobe.py`）、
並行して別コンテナが rate 10/s で 400 行、その後に別コンテナで 50 行。

| 確認 | 結果 |
|---|---|
| 同じファイルの前後の行（`long300k-102434`） | app **100 / 100** |
| 長い行 | infra 1 文書（`collector_errors=line_too_long`・`truncated=true`） |
| 並行する別ファイル（`bg300k-…`、400 行） | **400 / 400** |
| 後続の別コンテナ（`after300k-…`） | **50 / 50** |
| Fluent Bit の CPU | 直後 1.42%、終了後 0.29%（修正前は 100% で停止） |
| `dropped`・`retries_failed` | 0 |

**I-18（Bulk 部分失敗の probe、正常 20 行・試験行 1・正常 20 行）**:

| kind | 届いた件数 / 41 | 試験行の扱い |
|---|---|---|
| `int_float` | 41 | そのまま格納 |
| `bool_number` | 41 | `collector_errors=retryable`（退避） |
| `attributes_scalar` | 41 | そのまま格納 |
| `int_overflow` | 41 | 格納（`ignore_malformed`） |
| `num_overflow`（JSON の `1e400`） | 40 + infra 1 | JSON 解釈失敗で infra（I-19 の経路） |
| `str_inf`（`"inf"`） | **41** | `collector_errors=duration_ms`（退避） |
| `str_nan`（`"nan"`） | **41** | `collector_errors=duration_ms`（退避） |

- probe の後に `failed to flush` は 0 件、`dropped`・`errors` 0。中間版（§2.4）の 0/41 は解消。

### 5.11 S-ROT（合格。一巡させた分の欠損は検知できない＝受け入れる欠損）

2026-10-06T10:26:26〜10:29Z、Fluent Bit は c5af8ff の Lua。tool コンテナを `APPTEST_LOG_MAX_SIZE=1m APPTEST_LOG_MAX_FILE=5` で起動。

| 試験 | 手順 | 結果 |
|---|---|---|
| 一巡しない量 | `fault-collector-rotation.sh 6000 200`（Fluent Bit を止めて 6000 行 → `.log`〜`.log.4` の5ファイルに分かれる → 再開） | **6000 / 6000**、`dupes` 0 |
| 一巡させる量 | Fluent Bit 停止中に 20000 行（pad 200）→ 再開 | **5238 / 20000**。残った最古のファイル `.log.4` の先頭が `seq=14762` で、届いた件数 = 20000 − 14762 と一致（消えたファイルの分だけ欠ける）。`dupes` 0、`dropped`・`retries_failed` 0、`catchup` behind 0 |
| 事前の検知 | Fluent Bit 停止中に 1000 行を出して `check-pipeline.sh --catchup-only` | `FAIL fluent-bit: 到達できない` で **rc=1**（停止は検知できる）。再開後 1000/1000 |

- 一巡で消えた行は Fluent Bit の metrics にも `check-pipeline`（残っているファイルしか見ない）にも出ない。
  ADR-0040 §5 の「受け入れる欠損」どおり。検知できるのは「Collector が止まっている／遅れている」ことまで。

### 5.12 S-DUP（合格。infra は重複する＝設計どおり）

2026-10-06T10:27〜10:31Z、c5af8ff の Lua。

| 試験 | 結果 |
|---|---|
| `loggen.py --count 1000 --duplicate-every 10`（json-file 1100 行、同じ `event_id` の再出力 100） | `sa count --term request_id=d1-… --expect 1000` → **1000**、`dupes` **0** |
| Bulk 中の OpenSearch 停止（3000 行・rate 200/s の 7 秒目に `docker stop`、20 秒後に start） | **3000 / 3000**、`dupes` **0** |
| 位置 DB の削除（Fluent Bit 停止 → volume `avp2-oslog-c2-log_fbstate` の `tail.db*` を削除 → 再開。`read_from_head=true` で全ファイルを先頭から読み直す） | app: 再送 31138 records（`proc_records`）に対し文書は **31033 → 31038**、index 全体の `dupes` **0**（同じ index 内は `event_id` = `_id` で吸収）。infra: **19205 → 39170**（`_id` を持たないので読み直した分だけ重複） |

- 位置 DB 削除後に app が +5 件になったのは、§5.9 の restart で欠けた 5 行（seq 719・720、717・718、711）。
  読み直し後は `fbrestart-*` の3つとも **2000 / 2000**。欠けた行は json-file に残っていたが、位置 DB はその先まで
  進んでいた、という V-8 の推定を裏づける。
- rollover をまたぐ再送は §5.13（S-IDX）の新スタックでは試していない（rollover 後に旧 index の行を読み直すと、
  新しい write index に別文書として入る。runbook §5 の `collapse` で吸収する手順のまま）。
- infra 系統の重複は設計どおり（`event_id` を持たない）。位置 DB を消す運用（runbook §7）では infra の件数が増える
  ことを runbook に明記する。

### 5.13 S-CAP（合格。ただし破棄件数の metrics は件数として信用できない）

2026-10-06T10:30:45〜10:34:24Z、`fault-capacity.sh 200000 400 2000`（tag `cap-103045-17590`）、buffer は tmpfs 64m・
`storage.total_limit_size` 48M、c5af8ff の Lua。

- ホストの `/`: 試験中 `915G 116G 753G 14%`（buffer は tmpfs。増えた分は試験コンテナの json-file）。
- アプリ側: OpenSearch 停止中に 200000 行を 129 秒で出し終えた（止まらない）。`catchup` は直後に behind 0（rotation の欠損なし）。
- 停止中: buffer 45.1M・chunk 55、`dropped_records_total{avp_app}` **0 → 155343**、`evicted from output queue to make room under
  storage.total_limit_size` のログ。
- 復旧後: 件数は **101829** で止まった（新しい側が届く）。`dupes` 0、`retries_failed` 0。復旧直後の `OpenSearch Security not
  initialized`（503）の間にも追い出しが続き、`dropped` は最終 **198218**。
- **件数の食い違い**: 届いた 101829 + `dropped` 198218 = 300047 で、出した 200000 を 100047 上回る。追い出しのログは 106 回・
  計 90611712 bytes で、届いた chunk（`succeeded`）と追い出した chunk の名前の重なりは 0。`dropped_records_total` は失った
  件数の実数としては使えない（増えたこと＝破棄が起きたことの検知には使える）→ V-9（§5.15）。中間版（§2.5）では逆に
  9941 件が説明できなかった。

### 5.14 S-IDX（合格。plan どおり新しいスタックで）

短縮 ISM で最初から作るため、ログ基盤をもう1つ新規に作った: project `avp2-oslog-c2-idx`（port 19223、秘密は c2 と共用、
`logging-stack.sh up` の既定 = `ism-test`、収集対象 `avp2-oslog-c2`）。メモリのため `avp2-oslog-c2-log` の OpenSearch・
Fluent Bit はこの間止めた。2026-10-06T10:35:10〜10:42:37Z、c5af8ff。

- bootstrap 再実行: `sa_admin snapshot --out a.json; ldc run --rm bootstrap; sa_admin snapshot --out b.json; diff` → **空**
  （`ISM policy avp-app unchanged`、alias も同じ）。
- alias 名・typo の実 index 化: admin で `PUT avp-app-test2-write/_doc/x` → **404**、`avp_fluentbit` で同じ → **404**、
  `avp_fluentbit` の `PUT /avp-app-test2`（作成）→ **403**、`PUT /zz-probe/_doc/x` → **403**。index は増えていない。
- rollover・delete（loggen 10 行のあと 20 秒間隔で `sa_admin ism --index 'avp-app-*' --policy avp-app` と `alias`）:

  | 時刻（UTC） | app の index と ISM の状態 | write index |
  |---|---|---|
  | 10:37:05 | 000001:hot | 000001 |
  | 10:38:26 | 000001:hot、000002 作成 | **000002** |
  | 10:40:28 | 000003 作成 | 000003 |
  | 10:41:28 | 000001 が削除へ（state なし） | 000003 |
  | 10:42:09 | **000001 が消えた**（000002・000003 が hot） | 000003 |

  全 index が `avp-app` の管理下、failed 0。

### 5.15 S-RB（第1・第2段は合格。第3段は未実施）

| 段 | 手順 | 結果 |
|---|---|---|
| 1. ログ基盤を止める | `avp2-oslog-c2-log` の OpenSearch・Fluent Bit を停止した状態で、`test_production_e2e.py` と `test_upload_workflow_persistence.py` を実行。実行開始 12 秒後に `avp2-oslog-c2-idx` を `logging-stack.sh down`（`down -v`、volume 0 個に） | **8 passed**（JSON 条件の run-each と同数）、35 秒 |
| 1'. 再開 | `ldc up -d --wait opensearch; ldc up -d fluent-bit` | 停止中に runner が書いた JSON 行 541 行が **541 文書**として届いた（位置 DB から続き）。`catchup` behind 0 |
| 2. `AVP_LOG_FORMAT=text` | §5.1 の text 条件（全 integration） | **137 passed, 6 skipped**（JSON・故障注入と同数）。runner の出力は stdlib の text 形式、app 系統の文書は出ない |
| 3. compose の `logging:` を戻す | 未実施 | 隔離 app の compose（`compose.apptest.yaml`）の logging 設定を差し替える手段が試験 harness に無い。logging driver の設定は Docker 側の変更でアプリのプロセスに影響しないこと、1・2 段でアプリの合否が変わらないことまでを確認 |
