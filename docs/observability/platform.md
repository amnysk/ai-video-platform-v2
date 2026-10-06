# ログ基盤（収集・検索側）の運用設計

ADR-0040 の §4〜§7 を、`deploy/logging/` の実装に落としたもの。ログの形式・語彙は
[log-contract.md](./log-contract.md)。版と digest は [VERSIONS.md](../../deploy/logging/VERSIONS.md)。

> ログは検索用の副本。業務状態・課金・再実行の判断は DB と Temporal で行う（INV-38）。
> 「ログが無い」は未実行・収集停止・buffer 待ち・rotation による欠損を区別できない。

## 1. 収集経路

```
アプリ（1行1 JSON を stdout）
  → Docker json-file（max-size 20m × max-file 5、labels で attrs に service / project / avp.logging）
  → Fluent Bit（compose project avp2-logging）
       tail <containers>/*/*-json.log*（位置 DB・filesystem buffer）
       Lua avp_route   : attrs の compose project が AVP_LOG_TARGET_PROJECT と完全一致しない行は捨てる
                         COLLECTOR_LINE_MAX_BYTES を超える行は切って infra（line_too_long）
                         avp.logging=app かつ stdout かつ `{` で始まる行 → app、それ以外 → infra
       parser(json)    : app の行を解釈
       Lua avp_app     : 型修復・@timestamp・event_id の保証・追加の安全化・Collector フィールド
       Lua avp_infra   : 安全化・UNSTRUCTURED_LINE_MAX_BYTES で切り詰め
  → OpenSearch（write alias avp-app-<env>-write / avp-infra-<env>-write、ingest pipeline で ingested_at）
  → Dashboards（使う時だけ起動）
```

- Fluent Bit は Docker socket を持たない。containers/ を read-only で mount し、コンテナ内 root で動く
  （uid 1000 では1ファイルも読めず無音。ADR-0040 §6）。外への経路の無い `internal` network にだけいる。
- 書き込み先の index は Fluent Bit 設定の固定値。レコードの `environment` で振り分けない。
- 型修復: 契約の型に合わない値は `attributes.collector_moved` へ退避し、名前を `collector_errors` に残す。
  未知のトップレベルキー・アプリが書いた Collector のフィールドも同じ。`@timestamp` が不正・欠落なら
  Docker の時刻に置き換えて `@timestamp_replaced`。JSON として壊れた行は `log_source=unstructured`・
  `json_parse_failed`。
- **app 系統の記録は必ず `event_id`（= `_id`）を持つ**。_id にできない値（512 bytes 超・制御文字）と、
  JSON でない行には Collector が `collector-…` を付ける。Fluent Bit 5.1.2 は `id_key` の値が無い record に
  直前の record の `_id` を使い回し、409（成功扱い）で黙って消える（実測）ため。
- `@timestamp` は Lua が record から外して record の時刻にし、出力側（`time_key`）が1つだけ書く。
  重複キーは ingest pipeline 経由で bulk 全体を 400 にする（実測）。

## 2. index・ISM・保持

| 系統 | write alias | 最初の index | rollover | 削除 | mapping |
|---|---|---|---|---|---|
| app | `avp-app-<env>-write` | `avp-app-<env>-000001` | primary shard 5GiB または 7日 | rollover 後 14日 | `avp-app-mappings`（生成物） |
| infra | `avp-infra-<env>-write` | `avp-infra-<env>-000001` | 1GiB または 3日 | rollover 後 7日 | `avp-infra-mappings`（生成物） |

- primary 1 / replica 0（single-node。高可用ではない。cluster は常に yellow）。
- **保持は index 単位**: app は最大 7日書き込まれた index が rollover 後 14日で消えるので、1件の記録は
  **約14〜21日 ＋ ISM の実行遅延**（既定 5分間隔＋jitter。cluster 停止中は進まない）残る。厳密な削除期限に
  使えない。
- bootstrap（`scripts/bootstrap.py`）の順序: `action.auto_create_index` の確認 → alias 名の実 index 化の
  検出（見つけたら**何も書かずに**失敗）→ ingest pipeline → component template → 環境ごとの index template
  （`plugins.index_state_management.rollover_alias`。rollover で作られる次の index も template から受け取る）
  → ISM policy（`ism_template` は `avp-app-*` / `avp-infra-*` だけ）→ 初期 index と write alias。
  何度流しても同じ状態になる。policy を変えても**管理中の index は旧版のまま**（ISM の仕様）。
- alias 誤作成・typo index の防止: `action.auto_create_index: "-avp-*,+*"` と、writer に index 作成権限を
  与えないことの二重（どちらも実測で 404 / 403）。
- `dynamic: false`。未知のキーは `_source` に残るが検索できない。数値・日付の型不整合は `_ignored` に出る
  （保存済み検索「Collector の修復・型不整合」）。

## 3. Fluent Bit の設定と監視項目

| 設定 | 値 | 理由（ADR-0040 §5） |
|---|---|---|
| tail `path` | `/containers/*/*-json.log*` | Collector 停止中に rotation したファイルも inode で続きから読む（実測 300/300、重複0） |
| `read_from_head` | true（導入時だけ false） | 新しく見つかったファイルを先頭から読む。false だと停止中に作られたファイルを失う |
| `db` / `db.locking` | `/fb-state/tail.db` / true | 位置の永続化 |
| `buffer_max_size` / `skip_long_lines` | 256k / on | 既定 32k を超える行で監視が止まるのを防ぐ。skip は監視する。Docker の partial を結合した後の行には効かない（下記） |
| output `write_operation` / `id_key` | create / event_id | 同じ index 内の再送は 409（成功扱い）で重複しない |
| `suppress_type_name` | on | OpenSearch 3.x は `_type` を 400 |
| `tls.verify` / `tls.verify_hostname` | on / on | `verify_hostname` は既定 off |
| `buffer_size` | 4M | 応答が溢れると item ごとの判定ができず chunk 全体を再送する |
| `retry_limit` / `scheduler.base` / `scheduler.cap` | 72 / 5 / 300 | 有限。§7 の許容停止時間 |
| `trace_error` / `trace_output` | off | エラー応答に値のプレビューが入る |
| `storage.total_limit_size` | 1G（`AVP_LOG_STORAGE_LIMIT`） | 超えたら古い chunk から破棄 |

監視 endpoint（internal network の中だけ。ホストからは `scripts/fb-metrics.sh [path]`）:
`/api/v1/health`、`/api/v2/health`、`/api/v1/storage`、`/api/v1/metrics/prometheus`、
`/api/v2/metrics/prometheus`。

確認に使う metrics（`/api/v1/metrics/prometheus`、5.1.2 で名前を確認）:

| metric | ラベル | 意味・異常の判定 |
|---|---|---|
| `fluentbit_input_files_opened_total` | `name="tail.0"` | 開いたファイル数。**0 は containers/ が読めていない**（無音の失敗） |
| `fluentbit_input_files_rotated_total` | `name="tail.0"` | rotation を追った回数 |
| `fluentbit_input_long_line_skipped_total` | `name="tail.0"` | `buffer_max_size` を超えて捨てた行。増えたら異常 |
| `fluentbit_input_long_line_truncated_total` / `fluentbit_input_multiline_truncated_total` | `name="tail.0"` | 切り詰め |
| `fluentbit_output_proc_records_total` | `name="avp_app"` / `"avp_infra"` | 送れた record |
| `fluentbit_output_retries_total` | 同上 | 再送の回数 |
| `fluentbit_output_retried_records_total` | 同上 | 再送した record 数 |
| `fluentbit_output_retries_failed_total` | 同上 | 再送上限を超えて諦めた chunk。増えたら異常 |
| `fluentbit_output_dropped_records_total` | 同上 | 破棄した record（再送上限・buffer 上限の evict）。増えたら異常 |
| `fluentbit_output_errors_total` | 同上 | 出力のエラー |

### 長い行（I-15）

Docker json-file はアプリの1行を 16KiB ごとの partial に分けて書く。tail の `buffer_max_size`・
`skip_long_lines` は**ファイル上の1行（= partial 1つ）**に効き、`multiline.parser: docker` が結合した後の行には
上限が無い（5.1.2 で実測: 300k の行が `long_line_skipped_total` 0 のまま filter へ届いた。service の
`multiline_buffer_limit` は 32KiB にしても効かず、`multiline_truncated_total` も 0。旧来の `docker_mode` も同じ）。

- 修正前: 結合後の 300k の行（英数字の長い連なり）で安全化の Lua パターンが O(n²) になり、Fluent Bit の
  イベントループが CPU 100% のまま止まった。**全ファイルの読み取りが止まり**、health は ok、skip 0、buffer の
  chunk も増えない（隔離環境で実測。担当C が発見、担当B が Fluent Bit 単体で再現）。
- 対策1（上限）: Lua `avp_route` が `log` を `COLLECTOR_LINE_MAX_BYTES`（262144 = `buffer_max_size`、
  `contracts/log_contract.py`）で切り、JSON として解釈せず infra 系統へ送る（`collector_errors=line_too_long`、
  `truncated=true`、message は `UNSTRUCTURED_LINE_MAX_BYTES` まで）。行の残りは失われる。
- 対策2（線形化）: 安全化の規則から、照合に失敗した開始位置ごとに末尾まで読み戻す形を除いた（userinfo・JWT・
  SQL parameters・PEM・URL の query）。262144 bytes の病的な入力（英数字・hex・`eyJ`・`http://`・
  `[parameters: ` の繰り返し等）で 1行 0.05 秒以下（修正前は 40k で 7.6 秒、O(n²)）。閉じていない
  `[parameters: ` と PEM は行末まで伏せる（修正前は伏せなかった）。空の user（`redis://:pw@`）も伏せる。
- 実測（Fluent Bit 単体、OpenSearch なし、出力 file、本物の fluent-bit.yaml と Lua）:

  | | 長い行の後の同じファイルの行 | 別ファイル（0.2秒ごと）の行 | Fluent Bit の CPU |
  |---|---|---|---|
  | 修正前・300k | 0 / 100 | 停止（40秒間 0 行） | 100% |
  | 修正後・300k（上限超え → infra、`line_too_long`） | 100 / 100 | 読み続けた（15秒で +75 行） | 1% 未満 |
  | 修正後・200k（上限内 → app、message は `[REDACTED]`） | 100 / 100 | 読み続けた | 1% 未満 |

- 観測: 切った行は infra の文書 `collector_errors:line_too_long` で検索できる。

`/api/v1/storage` の `storage_layer.chunks.total_chunks`（buffer に溜まっている chunk）、
`/api/v2/health` の `status`（`hc_*` の閾値を超えると `error`）。

## 4. TLS・権限・秘密の置き場所

- 秘密は repo の外: `AVP_LOGGING_SECRETS_DIR`（既定 `~/.config/avp-logging/<env>`、0700、各ファイル 0600）。
  `scripts/init-secrets.sh` が作る（既存は上書きしない）。
  - `fluentbit.pw` / `viewer.pw` / `dashboards.pw` / `dashboards-cookie.pw`
  - `fluent-bit-secret.yaml`（Fluent Bit が include する `env:`。writer のパスワード）
  - `pki/ca.{pem,key}`、`pki/node.{pem,key}`（SAN: `opensearch`・`localhost`・`127.0.0.1`）、
    `pki/admin.{pem,key}`（admin_dn。setup の one-shot にだけ mount）
- **読み取り用の資格情報の決まった場所**（確認スクリプト・隔離試験が使う）:
  ユーザー `avp_viewer`、パスワード `<AVP_LOGGING_SECRETS_DIR>/viewer.pw`、CA `<AVP_LOGGING_SECRETS_DIR>/pki/ca.pem`、
  URL `https://127.0.0.1:<AVP_LOGGING_OS_PORT>`。
- CA は `basicConstraints=critical,CA:TRUE`・`keyUsage=critical,keyCertSign,cRLSign`・SKI を持ち、node・admin
  証明書は SKI・AKI を持つ（I-16）。これが無いと Python 3.13 以降の `ssl.create_default_context()`
  （`VERIFY_X509_STRICT` が既定）や `openssl verify -x509_strict` が「CA cert does not include key usage
  extension」で拒否する。**2026-10-06 より前に作った証明書は再生成しないと直らない**（`init-secrets.sh` は既存の
  ファイルを上書きしない）: `pki/` の `ca.*`・`node.*`・`admin.*` を退避 → `init-secrets.sh` → `security-init` →
  `$C up -d --force-recreate opensearch fluent-bit`（Dashboards を使っていれば同じく）→ `securityadmin`。
  DN は変わらないのでパスワード・ロールはそのまま。
- 証明書はホストの openssl で作る（OpenSearch / Dashboards のイメージに openssl が無い）。named volume への
  配置と internal_users の hash 化は one-shot（`security-init`）で行う。DN は RFC2253 順
  （`CN=…,OU=avp2-logging`。逆順だと admin 証明書が 401 になる。実測）。
- パスワードは env で渡さない（`docker inspect` で見える）: Fluent Bit は secret ファイルの include、
  Dashboards は keystore（`dashboards-keystore`）、hash 化は one-shot 内の子プロセスの env だけ。
- ロール（`opensearch/security/roles.yml`）。実測（隔離環境）:

  | ユーザー | ロール | できる | できない（403 / 404） |
  |---|---|---|---|
  | `avp_fluentbit` | `avp_log_writer` | `avp-app-*`/`avp-infra-*` への bulk create | 検索・index 削除・index 作成・typo index への書き込み（404） |
  | `avp_viewer` | `avp_log_viewer` + `kibana_user` | 検索・index の状態・Dashboards・saved objects | 書き込み・削除 |
  | `avp_dashboards` | `kibana_server` | Dashboards サーバー（`config.yml` の `server_username`） | — |
  | admin 証明書 | super admin | bootstrap・securityadmin | （常駐サービスには mount しない） |

- 9200・5601 は 127.0.0.1 のみ。Fluent Bit の 2020 は公開しない（internal network の中だけ）。外から見る
  ときは SSH トンネル。

## 5. 確認スクリプト

`deploy/logging/scripts/check-pipeline.sh`（ホストで実行。OpenSearch の外から見る）。異常は `FAIL` 行と
非0終了。前回からの増分は `~/.local/state/avp-logging/` に持つ。

- Fluent Bit: health、`files_opened_total == 0`、`dropped_records_total`・`retries_failed_total`・
  `long_line_skipped_total` の増分、buffer の chunk 数（`--max-chunks`、既定 2000）
- **追いつき**（I-17、`scripts/catchup.py`）: 位置 DB の offset と、収集対象 project（`--target-project`、既定
  `$AVP_LOG_TARGET_PROJECT` か `avp2`）の各コンテナの `*-json.log*` の大きさの差。位置 DB（volume
  `<project>_fbstate`）は read-only で mount した one-shot が **WAL ごと**一時ディレクトリへ複製し、ホストで
  その複製を読む（Fluent Bit は `db.locking` で DB を排他的に開いている。元の DB は開かない・止めない）。
  位置 DB に無いファイルは全量を未読と数える。差が `--max-behind-bytes`（既定 262144 = 1行の上限）を超えたら
  異常。対象は `docker ps -a --filter label=com.docker.compose.project=<target>` のコンテナだけ。
- **tail の停滞**（I-15）: 未読が残っているのに `input_records_total{name="tail.0"}` が前回の確認から
  増えていなければ異常（5秒おいて読み直してから判定）。Fluent Bit のイベントループが止まると health は ok、
  skip も chunk も変わらないので、これ以外では lag でしか見えない。対処は Fluent Bit の restart と、
  原因の行の特定（`collector_errors:line_too_long`・該当コンテナのログ）。
- `--catchup-only`: Fluent Bit と追いつき・停滞だけを見て終わる（deploy の前。§6）
- OpenSearch: 到達性・cluster の状態、**系統ごと**の最終 `ingested_at` からの経過（app は `--max-lag-min`、
  既定 90、infra は `--max-lag-infra-min`、既定 60。infra の行で app の停止が隠れないように）。app の既定は
  watchdog の周期（`DEFAULT_WATCHDOG_CRON` = `35 * * * *`、毎時）+ 30分: 静かな日の app 系統は毎時の
  watchdog の行だけになり、既定を周期と同じ 60 にすると次の実行の取り込みと確認（5分ごと）が競って誤報する
  （I-13）。infra は temporal 等が絶えず書くので 60 のまま、
  直近24時間の app 文書のうち `environment` が index の env と違う件数（> 0 で異常。アプリの `.env` の
  `AVP_ENVIRONMENT` の書き忘れ・取り違え）、直近24時間に Collector が切った長い行
  （`collector_errors:line_too_long`、> 0 で WARN。終了コードは変えない）、index サイズ、ディスク使用率（85% 以上で異常）
- 資格情報は viewer のパスワードを 0600 の一時 config で `curl -K` に渡す（argv に出さない）
- 証明書: CA・ノード証明書の残り 30日未満
- ホスト: Docker root のディスク（90% 以上で異常）、MemAvailable
- `--enforce-memory`: MemAvailable < 2GiB で Dashboards、< 1.5GiB で OpenSearch を止める
  （`--project` のコンテナだけ。アプリの project 名を渡すと拒否する）

systemd user timer の例: `deploy/logging/systemd/avp-logging-check.{service,timer}`（5分ごと。インストールは
しない。手順はファイルの先頭）。結果は `journalctl --user -u avp-logging-check`。通知先の接続は範囲外。

## 6. 導入・停止・rollback（設定面）

```bash
C="docker compose -f deploy/logging/compose.logging.yaml"
deploy/logging/scripts/init-secrets.sh --env prod          # 1. 秘密（repo 外）
$C run --rm security-init                                  # 2. 証明書・internal_users・sentinel
$C up -d --wait opensearch                                 # 3. OpenSearch
$C run --rm bootstrap                                      # 4. index 基盤（冪等）
AVP_LOG_READ_FROM_HEAD=false $C up -d fluent-bit           # 5. 位置 DB を作る（既存の大きなログを読まない）
#    files_opened が増えたのを scripts/fb-metrics.sh で確認してから
$C up -d fluent-bit                                        # 6. read_from_head=true で作り直す
$C --profile dashboards-setup run --rm dashboards-keystore # 7. Dashboards（使うときだけ）
$C --profile dashboards up -d dashboards
$C --profile dashboards-setup run --rm dashboards-import
```

- **本番のアプリの `.env`（`compose.yaml` のディレクトリ）に `AVP_ENVIRONMENT=prod` を書く**。compose は
  既定値を持たない（I-5: 既定 `dev` だと本番の行が `avp-app-prod-*` に `environment=dev` で入った）。
  未設定なら整形器が `unknown` にする。Collector の `AVP_LOGGING_ENV`（index 名）とは別の設定なので、
  食い違いは `check-pipeline.sh` が検出する（§5）。
- アプリ側の変更（`compose.yaml` の `logging:`・label・env）は、コンテナの**再作成**で反映される
  （次の deploy-workers）。再作成で消える未読の json-file は検知できない欠損になるので、deploy の前に
  `check-pipeline.sh` で Collector が追いついている（buffer chunk が 0 付近、lag が小さい）ことを見る。
- 位置 DB が無い状態で `read_from_head=true` のまま起動しようとすると `fluent-bit-guard` が止める。
  sentinel の無い volume（project 名の違いで新しく作られた空の volume）でも OpenSearch・Fluent Bit は起動しない。
- パスワード・ロールの変更: `init-secrets.sh`（変えるファイルを消して再生成）→ `security-init` →
  `$C run --rm securityadmin`。初回の security index は `allow_default_init_securityindex` が作る。
- 長い停止: OpenSearch を止めるときは Fluent Bit も止める（位置 DB から再開でき、rotation が一巡する
  まで欠損しない）。
- rollback: `$C down`（volume は残る）。アプリの挙動は変わらない。アプリ側は `AVP_LOG_FORMAT=text` で
  従来の形式に戻せる。消すなら `$C down -v`（検索用ログは失われる。SSoT ではない）。

## 7. 資源・共存条件・許容停止時間

- 上限: OpenSearch heap 1GiB・`mem_limit`=`memswap_limit` 2.5GiB、Dashboards 1GiB（profile）、
  Fluent Bit 256MiB。`oom_score_adj` は Dashboards 900 > OpenSearch 800 > Fluent Bit 600（本番 worker より先に落ちる）。
- 実測（隔離環境、2026-10-05、heap 512m）: OpenSearch RSS 約 1.03GiB、Dashboards 約 230MiB、Fluent Bit 約 6MiB。
  heap 1GiB では 1.6〜2GiB 程度を見込む（本番の値は導入時に実測して更新する）。
- 共存の条件（ADR-0040 §7）: MemAvailable < 2GiB で Dashboards を、< 1.5GiB で OpenSearch を止める
  （`check-pipeline.sh --enforce-memory`）。満たせなければ `deploy/logging` を別ホストで動かし、本番ホストには
  Fluent Bit だけを置く（`AVP_LOGGING_OPENSEARCH_HOST` を差し替え、Fluent Bit を外へ出られる network にも
  繋ぐ override を足す）。
- **許容できる OpenSearch 停止時間**: retry の上限（72回）と buffer の上限（1GiB）の短い方。
  **実測: 72回の再送は約 2時間49分で尽きた**（隔離環境、2026-09-30 03:37 → 06:26。backoff は base 5秒〜cap 300秒の
  乱数で、平均 約140秒/回）。ADR-0040 §5 の「約6時間」は cap × 回数の上限で、実際はその半分程度になる。
  これを超える停止では、その間に溜まった chunk が再送上限で破棄される（`retries_failed_total`・
  `dropped_records_total`）。数時間以上止めるときは Fluent Bit を先に止める。

## 8. 欠損の検知範囲

| 欠損 | 検知 |
|---|---|
| 再送上限・buffer 上限による破棄 | `dropped_records_total` / `retries_failed_total` の増分（check-pipeline） |
| containers/ が読めない（mount 不成立・権限） | `files_opened_total == 0` |
| `buffer_max_size` を超える行（partial でない行） | `long_line_skipped_total` の増分 |
| `COLLECTOR_LINE_MAX_BYTES` を超える行（partial を結合した行） | infra の `collector_errors:line_too_long`（行の残りは失われる） |
| 収集・送信の停止 | 系統ごとの最終 `ingested_at` の lag（業務が止まっているのと区別できない） |
| tail の停滞（イベントループが止まる。I-15） | 未読が残り `input_records_total{tail.0}` が増えない（check-pipeline） |
| Collector が追いついていない | 位置 DB の offset とファイルの大きさの差（check-pipeline の追いつき。deploy 前は `--catchup-only`） |
| `environment` の取り違え（`.env` の書き忘れ） | 直近24時間の不一致件数（check-pipeline） |
| 型不整合・時刻の置換 | 文書の `collector_errors` / `_ignored`（失われない） |
| Collector 停止中に rotation が一巡した分 | **検知できない** |
| コンテナ再作成で消えた未読ファイル | **検知できない**（deploy 前の `check-pipeline.sh --catchup-only` で減らす） |
| rotation 境界をまたぐ partial 行 | **検知できない**（結合されず順序も入れ替わる） |
| `event_id` の衝突（別の記録が同じ ID） | **検知できない**（409 は成功扱い）。導出の unit test で防ぐ |

## 9. 隔離試験の再現（担当C 向け）

本番の `avp2` とその volume・network には触れない。project 名・ポート・秘密の置き場所を固有にする。
volume と network の名前は `<project>_<key>` になる（`-p` で分かれる）。

```bash
export AVP_LOGGING_ENV=test
export AVP_LOGGING_SECRETS_DIR=$HOME/.config/avp-logging-test/c
export AVP_LOG_TARGET_PROJECT=avp2-oslog-c-app      # 収集対象（完全一致）
export AVP_LOG_CONTAINERS_DIR=$HOME/.local/share/docker/containers
export AVP_LOGGING_OS_PORT=19202 AVP_LOGGING_OSD_PORT=15603
export AVP_LOGGING_OS_HEAP=512m AVP_LOGGING_OS_MEM_LIMIT=1400m
export AVP_LOG_BUFFER_TMPFS_SIZE=64m AVP_LOG_STORAGE_LIMIT=48M AVP_ISM_JOB_INTERVAL_MIN=1
C="docker compose -p avp2-oslog-c -f deploy/logging/compose.logging.yaml -f deploy/logging/compose.test.yaml"
deploy/logging/scripts/init-secrets.sh --env test --dir "$AVP_LOGGING_SECRETS_DIR"
$C run --rm security-init && $C up -d --wait opensearch && $C run --rm bootstrap
AVP_LOG_READ_FROM_HEAD=false $C up -d fluent-bit && $C up -d fluent-bit
deploy/logging/scripts/fb-metrics.sh --project avp2-oslog-c /api/v1/storage
AVP_LOGGING_OS_PORT=19202 deploy/logging/scripts/check-pipeline.sh --env test \
  --project avp2-oslog-c --secrets-dir "$AVP_LOGGING_SECRETS_DIR"
$C --profile dashboards --profile setup --profile dashboards-setup down -v   # 片付け
```

- `compose.test.yaml`: Fluent Bit の buffer を `size=$AVP_LOG_BUFFER_TMPFS_SIZE` の tmpfs に、bootstrap が
  `opensearch/ism-test`（2件または2分で rollover、rollover 後3分で削除）と ISM の実行間隔
  `AVP_ISM_JOB_INTERVAL_MIN` 分を使う。
- 本番の containers/ 全体ではなく試験コンテナだけを見せたいときは、Fluent Bit と `fluent-bit-guard` の
  `/containers` を空ディレクトリにし、`<containers>/<id>:/containers/<id>:ro` を override で足す（空ディレクトリ側に
  `<id>` の mountpoint を先に作る。read-only の親の下には作れない）。
- 試験コンテナの logging はアプリと同じ: `json-file`、`labels: com.docker.compose.service,com.docker.compose.project,avp.logging`、
  `tag: "{{.Name}}"`、app のサービスに label `avp.logging: app`。
- 書き込み・権限の確認: writer は `avp_fluentbit` / `fluentbit.pw`、admin は `pki/admin.{pem,key}`（curl `--cert/--key`）。

## 10. 実測記録（担当B、隔離 project `avp2-oslog-b`、2026-09-30〜10-05）

| 確認 | 結果 |
|---|---|
| bootstrap を2回 | 2回目は全て `unchanged` / 既存 alias の確認だけで成功。5日後の再実行で infra が `-000002` に rollover 済み（ISM の 3日条件が実際に動いた） |
| alias 名の実 index 化 | `avp-app-probe-write` を実 index として作ると bootstrap が `FAIL … alias 誤作成` で終了（exit 1）。検出は何かを書く前 |
| typo index | writer で `avp-app-typo` に書くと 404（auto_create で拒否） |
| writer の権限 | 検索・index 削除・index 作成が 403 |
| viewer の権限 | 書き込み・削除が 403。検索・Dashboards は可 |
| JSON 行が検索できる | 試験コンテナの行が `avp-app-test-*` に、label の無いコンテナの行（DSN を伏せ字化）が `avp-infra-test-*` に入った |
| 型不整合1件 | Lua が退避し 400 にならない（`collector_errors=[http_status,retryable,scene_id]`）。Lua を通さない bulk では型不整合の item だけ 400、他は 201 |
| `_id` 512 bytes 超 | 修正前: request 全体が 400 → 72回再送（2時間49分）→ 正常2件を含む3件を破棄。修正後: `collector-…` の ID で格納、前後の行も格納 |
| `id_key` の値が無い record | 修正前: 直前の record の `_id` で送られ 409 で消えた。修正後: 必ず ID を持つ |
| ミリ秒 | 修正前: `.001` が `.000` で保存。修正後: 一致 |
| Dashboards | keystore で起動、`api/status` 200、saved objects 10件を import（成功） |
| check-pipeline | 全項目 OK で exit 0 |

## 11. 版の更新

VERSIONS.md の表と compose の digest を同じ commit で変え、`tests/contract/test_logging_platform_config.py`
（digest の一致）・`tests/integration/test_logging_collector_lua.py`（新しい Fluent Bit で filter 列）を流し、
§10 の確認を隔離環境でやり直す。Fluent Bit を上げるときは `id_key` 欠落時の `_id` 使い回し（§1）と
bulk 応答の扱い（VERSIONS.md）を再確認する。
