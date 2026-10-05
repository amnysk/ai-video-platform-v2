# ADR-0040: 構造化 JSON ログと OpenSearch の検索用副本

## Status

Accepted（2026-09-30。設計レビューと独立再確認: 独立レビュー D-1〜D-20、producer A-1〜A-18、consumer B-1〜B-12
を反映。実装レビューの結果は docs/observability/review-log.md）

## Context

実査（2026-09-30、読み取りのみ）:

- 本番は `claude/daily-hardening` の worktree から `ea153c8` で稼働（全アプリコンテナの
  `org.opencontainers.image.revision` と `AVP_GIT_REVISION` が一致）。rootless Docker 29.8.0
  （root `~/.local/share/docker`）、compose v2.40.3、全コンテナが **json-file driver・オプション無し
  （ローテーション無し）**。最大のコンテナログは約 77MB。
- アプリログは `logging.basicConfig(level=INFO)` の非構造テキスト（13か所で個別に初期化）。
  uvicorn は CLI 起動で独自の handler（access log は query 付き path）。Temporal Core（Rust）のログは
  Python logging を通らず console に直接出る（`LoggingConfig.default` の `forwarding=None`。
  実測では stdout に ANSI 色つきの非 JSON 行。2026-10-05 訂正、当初「stderr」と記載）。
- 2026-09-22 以降の 403（ADR-0030）では診断が DB の `error_summary` 文字列頼みで、どの試行・
  どのシーン・どの操作（submit / storage token）かをログから辿れなかった。
- 既存の監視: DB の `operational_anomalies` と watchdog（ADR-0027/0031）、`avp.anomaly` logger。
  `compose.yaml` の `observability` profile の Prometheus/Grafana は scrape 先の無い雛形で未稼働。
- 資源: RAM 14GiB、MemAvailable 約7.0GiB、**swap 4GiB が満杯**（主にデスクトップ側のプロセス）、
  12 CPU、`/` NVMe ext4 767GiB 空き、MinIO と作業領域は別の HDD（`/mnt/minio-hdd`）。本番コンテナ16個の
  RSS 合計は約 2.4GiB。`vm.max_map_count=1048576`。memlock hard 8MiB（rootless のため
  `bootstrap.memory_lock` は不可）。docker は io.weight / io.max 非対応（I/O を絞れない）。

## Decision

**アプリは安全化済みの1行1 JSON を stdout に出し、Docker json-file（ローテーション付き）を
Fluent Bit が read-only で tail して OpenSearch の検索用副本へ送る。OpenSearch は業務の自動判断に使わない。**

### 1. 経路

`Python logging（JSON 整形・安全化・文脈）→ stdout → Docker json-file（max-size 20m × max-file 5、
labels で compose サービス名と avp.logging を各行の attrs に付与）→ Fluent Bit（tail + 位置 DB +
filesystem buffer + Lua の型検査・安全化）→ OpenSearch（write alias、明示 mapping、ingest pipeline で
ingested_at）→ Dashboards`。

- 業務の経路に OpenSearch への同期 I/O を入れない。アプリが書くのは stdout だけ。
- json-file は既定の blocking モードのまま（ローカルディスクへの追記。non-blocking の黙った欠損を避ける）。
  ディスク枯渇時はアプリの stdout 書き込みも失敗し得る。Workflow スレッドで stdout が詰まると SDK の
  deadlock 検知（2秒）で workflow task が失敗し得る。どちらもホストの容量監視の対象。
- Docker socket は渡さない。コンテナの識別は json-file の `labels`/`tag` が各行に書く `attrs`
  （`com.docker.compose.service`/`project`、`avp.logging`）とアプリ自身の `service_name` で行う。
- **振り分けは label**: `avp.logging=app` の行を JSON として解釈して app 系統へ。それ以外
  （postgres・temporal・minio・JSON でない行・stderr）は `log_source=unstructured` として Lua で安全化・
  切り詰め（`UNSTRUCTURED_LINE_MAX_BYTES`）して infra 系統へ。compose project は**完全一致**で絞る。
- Temporal Core のログは `Runtime(telemetry=TelemetryConfig(logging=LoggingConfig(forwarding=
  LogForwardingConfig(logger=logging.getLogger("temporalio.core")))))` を最初の connect より前に
  既定 Runtime として設定し、Python logging に転送する（client・worker の全接続が同じ Runtime）。
- API は `python -m apps.api.serve`（`uvicorn.run(log_config=None, access_log=False)` + 共通 setup）で
  起動し、access log は `api.request.completed` で置き換える。Dockerfile の app stage にも
  `AVP_GIT_REVISION` を焼く（現状 api/migrate の git_sha が取れない）。

### 2. ログ契約

[docs/observability/log-contract.md](../observability/log-contract.md)。語彙・型・上限・設定キーは
`contracts/log_contract.py` が唯一の宣言元。mapping と Collector の型表はそこから**生成**
（`scripts/gen_log_mapping.py`）し、生成物との drift を contract test で検査する。

### 3. 実装の境界（呼び出し方向）

- `contracts/log_contract.py`: 語彙・上限・設定キーだけ。
- `infrastructure/logging/`: 整形・安全化・文脈（contextvars）・発行ヘルパー・Temporal の Activity
  interceptor・Runtime のログ転送・API middleware。**import 時に副作用を持たない**（handler の登録は
  `configure_logging()` を呼んだ時だけ）。Workflow の sandbox へは `imports_passed_through` 経由でのみ
  入る（sandbox 内で再 import されると handler が分裂し workflow task が失敗することを実測）。
- `domain` はログを出さない。**Workflow のコードは `infrastructure` を import しない**
  （`tests/architecture/test_research_workflow_determinism.py` の方針を全 workflow に広げる）。
  Workflow は `workflow.logger` と `extra={"avp": {...}}` だけを使う。
- 発行するのは `apps`・`workers`・`infrastructure`。どこも OpenSearch を import・接続しない
  （architecture test）。
- 予約台帳・成果物の commit 後イベントは、commit の直後に発行する（log-contract §9）。発行の失敗は握る。
- `deploy/logging/`: Fluent Bit・OpenSearch・Dashboards の設定と bootstrap。**別の compose project**
  （`avp2-logging`）。アプリの compose の変更は `logging:`（json-file オプション・labels）、
  `AVP_SERVICE_NAME`・`AVP_ENVIRONMENT`・`AVP_LOG_FORMAT` の env、API の起動コマンドだけ。
- 既存の伏せ字処理（`codex_cli.mask_secrets`・`youtube.uploader.redact`・upload の scrub・
  `url_guard.redact_url`）は各 adapter の責務として残す。ログの整形器はそれらの出力にも同じ規則を
  重ねて適用する（二重適用で壊れない形にする）。統合は別課題（今回の差分を広げない）。

#### 新しい設定キーの grep（AGENTS §8、2026-09-30 実行）

`grep -rn "<key>" apps/ workers/ domain/ infrastructure/ contracts/ docs/ compose.yaml Dockerfile scripts/`

| キー | 件数 | 分類 |
|---|---|---|
| `AVP_SERVICE_NAME` | 2 | 無関係（この ADR 草案の記述のみ） |
| `AVP_ENVIRONMENT` | 2 | 無関係（同上） |
| `AVP_LOG_FORMAT` / `AVP_LOG_LEVEL` | 0 | — |
| `AVP_GIT_REVISION` | 9 | 書き手: Dockerfile:88（worker stage）。読み手: `worker_entry.py:46`、`fal_storage.py:64`、docs。今回 app stage に書き手を1つ足す |
| `avp.logging`（label） | 0 | — |

### 4. index と保持

- 用途×環境の系統: `avp-app-<env>`（アプリ JSON）と `avp-infra-<env>`（unstructured）。書き込みは
  write alias `avp-app-<env>-write`、実 index は `avp-app-<env>-000001` から。Episode・service 別の
  index は作らない。書き込み先は Fluent Bit 設定の**固定値**（レコードの値で index を決めない）。
- 初期値: primary 1 / replica 0（single-node、高可用ではない）、app は rollover 5GiB（primary shard）
  または 7日・rollover 後 14日で削除、infra は 1GiB または 3日・rollover 後 7日で削除。
  保持は index 単位なので、イベントごとの厳密な14日ではなく**約14〜21日＋ISM 実行遅延**（cluster 停止中は
  さらに延びる）。
- mapping は `dynamic: false`（未知のキーは `_source` に残るが index しない）、数値・日付は
  `ignore_malformed`（`_ignored` で検出可能）。boolean・keyword・text は `ignore_malformed` 非対応
  （実測）なので Collector の Lua が型の合わない値を `attributes.collector_moved` へ退避し
  `collector_errors` に記録する。`@timestamp` は `ignore_malformed` にせず、不正・欠落なら Collector が
  Docker の時刻で置き換え `collector_errors=@timestamp_replaced`。
- component template（生成した mapping）+ 環境ごとの index template（`rollover_alias`）+ ISM policy
  （`ism_template` は対象 prefix だけ）+ ingest pipeline + 初期 index と alias を、この順で冪等に
  bootstrap する。全 index を対象にする操作はしない。
- alias 名の実 index 化・typo index の防止: `action.auto_create_index: "-avp-*,+*"`（system index に
  影響しないことを実測）と、Collector ロールに index 作成権限を与えないことの二重。

### 5. 収集の欠損と障害時の挙動

- tail は `path: <containers>/*/*-json.log*`、`read_from_head: true`、位置 DB（永続・`db.locking`）。
  Collector 停止中に rotation したファイルも inode で続きから読める（実測: 300/300、重複 0）。
  導入時だけ `read_from_head: false` で位置 DB を作り、既存の大きなログ（約77MB）を全量取り込まない。
- **検知できない欠損**（受け入れる）: Collector 停止が `max-size × max-file ÷ 出力速度` を超えて
  rotation が一巡した分、コンテナの**再作成**（deploy-workers）で消えた未読ファイル、rotation 境界を
  またぐ partial 行（結合されず順序も入れ替わる、実測）。deploy 前に確認スクリプトで Collector の
  追いつき（位置 DB の offset とファイルサイズの差）を見る手順を runbook に入れる。
- 出力: `write_operation create`、`id_key event_id`、`suppress_type_name on`（OpenSearch 3.x は `_type`
  を 400 で拒否）、`buffer_size 4M`（応答が溢れると部分失敗を解析できず chunk 全体を再送する）、
  `retry_limit 72`・`scheduler.base 5`・`scheduler.cap 300`（有限。不正1件による永久滞留を防ぐ。400 は Lua の型検査で実質ゼロにする前提）、`trace_error off`（応答に値のプレビューが入る）。
  採用版は bulk の item ごとに 2xx/409 を成功扱いにし、失敗 item だけを再送する（ソースと実測）。
  400 と 429 は区別されない。上限を超えた record は破棄され `dropped_records_total`・
  `retries_failed_total` に出る。
- **許容できる OpenSearch 停止時間**は retry 上限と buffer 上限の短い方で決まる。上の設定で約6時間（backoff の cap 300秒 × 72 回。jitter あり）、buffer 1GiB は現在の出力量なら数日分なので、retry 側が律速する。これを超える停止では古い record から破棄される（`dropped_records_total` で検知）。長く止めるときは Fluent Bit も止める（位置 DB から再開でき、rotation 一巡までは欠損しない）。実測で調整する。
- tail: `buffer_max_size 256k`、`skip_long_lines on`（既定 32k を超える行でファイルの監視が止まるのを防ぐ。Docker は `<` を `\u003c` に escape するので包装後の行は生の行より膨らむ）。skip は `long_line_skipped` 系の metrics で監視する。
- buffer: filesystem、`storage.total_limit_size` 1GiB（超えたら古い chunk から破棄）、メモリ上限つき。
  **有限バッファでは無欠損と無停止を同時に保証できない**ので、業務を止めない側を選び、破棄・再送・
  滞留を監視する。
- ingest pipeline（`default_pipeline`、`ingested_at` を付けるだけ）は、bulk 内に JSON として壊れた文書が
  1件あると bulk 全体を 400 にする（実測）。Fluent Bit が JSON を再エンコードして送る経路でのみ使い、
  これを integration test で固定する。
- 欠損・停止の検知は OpenSearch の**外**: ホストで動く確認スクリプト
  （`deploy/logging/scripts/check-pipeline.sh`、systemd user timer の例つき）が Fluent Bit の
  metrics・storage と OpenSearch の最終 `ingested_at`・証明書の期限・ディスクを読み、異常なら非0で
  終了し journal に残す。通知先の接続は今回の範囲外。「ログが無い」で未実行と判断せず DB・Temporal と
  照合する手順を runbook に書く。

### 6. 安全

- 秘密は stdout に出す前に除去（log-contract §7）。Collector の Lua は追加防御。
- **Collector の権限**: rootless ではコンテナ内 root がホストの所有者に写像されるので、containers
  ディレクトリを read-only で mount すると各コンテナの `config.v2.json`（Env＝本番の秘密）も読める
  （実測。uid 1000 で動かすと1ファイルも読めず、しかも無音）。Docker socket（コンテナ作成＝ホスト制御）
  よりは狭いが、**全サービスの秘密を読める権限である**ことを受け入れて次で囲う: mount は
  `containers/` だけ（docker root 全体は渡さない）、`read_only` rootfs、`cap_drop: [ALL]`、
  `no-new-privileges`、image digest 固定、tail の path は `*-json.log*` だけ、ネットワークは
  OpenSearch 向けの internal network と 127.0.0.1 の metrics だけ、`files_opened_total == 0` を異常として検知。
- OpenSearch は security plugin の TLS・認証を有効。`DISABLE_INSTALL_DEMO_CONFIG=true`、イメージ同梱の
  demo ユーザー入り `internal_users.yml` 等は必ず自前に差し替える。CA・ノード・admin 証明書は
  one-shot コンテナで生成し named volume に置く（DN は RFC2253 順）。
- ユーザーと権限: `avp_log_writer`（`avp-app-*`/`avp-infra-*` への bulk/index だけ。index 作成・検索・
  削除なし）、`avp_log_viewer`（読み取り + Dashboards 利用）、Dashboards のサーバーユーザー、
  admin は bootstrap と securityadmin の one-shot だけ（admin 証明書はその時だけ mount）。
- パスワードは repo 外のファイル（`~/.config/avp-logging/`、0600）から one-shot で hash を作り、env では
  渡さない（`docker inspect` で見えるため）。Fluent Bit は secret ファイルを include、Dashboards は keystore。
- 9200・5601・Fluent Bit の 2020 は 127.0.0.1 のみ。外から見るときは SSH トンネル。

### 7. 版と資源

- **OpenSearch 3.8.0 / Dashboards 3.8.0 / Fluent Bit 5.1.2** を digest で固定（`deploy/logging/VERSIONS.md`）。
  3.9.0 は公開1日で見送り。`tls.verify_hostname on` を明示（既定 off）。
- OpenSearch heap 1GiB・`mem_limit` 2.5GiB・`memswap_limit` 同値（swap 不使用）、Dashboards 1GiB
  （使う時だけ起動する profile）、Fluent Bit 256MiB。ログ基盤のコンテナは `oom_score_adj` を上げ、
  OOM 時に本番 worker より先に落ちるようにする。performance analyzer は無効。
- データは NVMe 上の named volume（MinIO の HDD と I/O を競合させない）。
- swap が満杯の現状では、**本番ホストへの共存は隔離試験での実測と、次の運用閾値を条件とする**:
  MemAvailable が 2GiB を下回ったら Dashboards を止め、1.5GiB を下回ったら OpenSearch を止める
  （Fluent Bit はバッファに溜め、上限を超えれば破棄する）。確認スクリプトが MemAvailable を報告し、systemd timer から `--enforce-memory` 付きで動かすと閾値で `avp2-logging` project のコンテナ**だけ**を止める（アプリには触れない。既定は報告のみ）。
  条件を満たせない場合は `deploy/logging` をそのまま別ホストで動かし、本番ホストには Fluent Bit だけを
  置く（`OPENSEARCH_HOST` を差し替えるだけの構成にする）。

### 不変条件

- **INV-38** ログの発行・収集・検索基盤の障害は業務処理を失敗・停止させず、業務状態・課金・例外の型を
  変えない。ログ検索の結果を課金判定・再実行・再開・投稿の自動判断に使わない。
- **INV-39** 秘密情報・provider 応答全文・prompt 全文・メディアのバイト列は、Python logging を経由する
  全ての記録（第三者 logger・未捕捉例外・Temporal Core の転送を含む）について stdout に出る前に除去される。
- **INV-40** Workflow のログ発行は決定性を崩さず（時刻・乱数・UUID・I/O を足さない、Workflow のコードは
  `infrastructure` を import しない）、replay で業務イベントを重複発行しない。

## Alternatives

- **アプリから OpenSearch へ直接送る（Python handler / OTLP）**: 業務プロセスに同期・非同期の
  ネットワーク I/O と再送バッファを持ち込み、停止時に動画処理へ影響し得る。採らない。
- **Docker の fluentd logging driver**: driver を変えると `docker logs` が使えなくなり、Collector 停止時に
  driver の挙動（blocking/non-blocking）が業務に直結する。json-file を残し tail する方が切り離せる。
- **Docker socket からメタデータを取る（docker_events / filter_docker）**: socket はコンテナ作成＝ホスト
  制御に等しい。containers/ の read-only mount も秘密を読めるが、作成・実行はできない（§6）。
- **Collector を uid 1000 で動かす**: rootless では containers/ に入れず無音で何も読めない（実測）。
- **Data Prepper / Kafka / Tempo / Prometheus の新設**: 現在の量（1日数本の Episode）と Logs 中心の要件で
  必要性が実証されていない。ingested_at は ingest pipeline で足りる。
- **Elasticsearch / Loki**: 指示と既存設計に合わせ OpenSearch。Loki はラベル以外の全文・フィールド検索が
  弱く、scene 単位の照合に向かない。
- **structlog 等のライブラリ追加**: 標準 logging の Formatter/Filter で要件を満たせ、第三者 logger も
  同じ経路で扱える。依存を増やさない。
- **Workflow 内で `workflow.uuid4()` で event_id**: 乱数列を消費し稼働中の workflow を非決定にする。
- **Workflow インスタンスに発行オブジェクトを持たせる**: Workflow から `infrastructure` を import する
  ことになり、既存の決定性の architecture test と衝突する。整形器側で導く。
- **SQLAlchemy の `hide_parameters=True`**: DB に残る `error_summary` の内容まで変わる（業務の診断記録の
  変更）。ログの整形器で `[parameters: …]` を落とす。
- **Episode 別・日次 index**: shard が乱立し single-node の heap を圧迫する。

## Consequences

良い:

- Episode・scene・attempt・provider ジョブを横断して検索でき、403/422/fallback/resume/課金予約を
  シーン単位で照合できる。
- ログ基盤はアプリと別 project で、止めても・消してもアプリの挙動は変わらない。アプリ側も
  `AVP_LOG_FORMAT=text` で従来形式に戻せる。
- json-file のローテーションで、現状の無制限なコンテナログ増加が止まる。

悪い・引き受けた負債:

- 有限バッファのため、長時間の OpenSearch 停止ではログが欠損し得る。Collector 停止中の rotation 一巡・
  コンテナ再作成による欠損は Collector からは検知できない（§5）。
- rotation の導入で、これまで無期限に残っていたコンテナログ（commit 前の submit 受理行を含む）が
  約100MB/コンテナで消えるようになる。照合の手がかりは OpenSearch 側の保持（約14〜21日）で持つ。
- 文書 ID による重複抑制は同一 index 内だけ。rollover をまたぐ再送と、workflow task の失敗後の再発行は
  重複し得る。
- 保持期限は index 単位で最大約21日＋遅延。厳密な削除期限が要る用途には使えない。
- single-node / replica 0 のため、ディスク故障で検索用ログは失われる（SSoT ではないので業務は影響なし）。
- Collector は全コンテナの設定ファイル（秘密を含む）を読める権限を持つ（§6 の囲いで受け入れる）。
- アプリの compose に `logging:` を足すと、適用にはコンテナの再作成が要る（次の deploy-workers で反映）。
- 本番ホストはメモリに余裕が少なく、共存の可否は実測と運用閾値に依存する。
- CLI provider の stderr を含む既存の例外文は、パターンで検出できない prompt 断片を含み得る（長さ上限のみ）。
