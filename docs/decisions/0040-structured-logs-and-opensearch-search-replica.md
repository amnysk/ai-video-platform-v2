# ADR-0040: 構造化 JSON ログと OpenSearch の検索用副本

## Status

Proposed（2026-09-30。設計レビュー後に Accepted へ）

## Context

実査（2026-09-30、読み取りのみ）:

- 本番は `claude/daily-hardening` の worktree から `ea153c8` で稼働（全アプリコンテナの
  `org.opencontainers.image.revision` と `AVP_GIT_REVISION` が一致）。rootless Docker 29.8.0
  （root `~/.local/share/docker`）、全コンテナが **json-file driver・オプション無し（ローテーション無し）**。
- アプリログは `logging.basicConfig(level=INFO)` の非構造テキスト（`INFO:logger:msg`）。
  `paid submit accepted ... ref={...}` のように provider の参照 JSON を丸ごと出している。
  uvicorn の access log は別形式。Episode・scene・attempt を横断して検索する手段が無い。
- 2026-09-22 以降の 403（ADR-0030）では診断が DB の `error_summary` 文字列頼みで、どの試行・
  どのシーン・どの操作（submit / storage token）かをログから辿れなかった。
- 既存の監視: DB の `operational_anomalies` と watchdog（ADR-0027/0031）、`avp.anomaly` logger。
  `compose.yaml` の `observability` profile の Prometheus/Grafana は scrape 先の無い雛形で未稼働。
- 資源（`free`/`df`/`docker stats`）: RAM 14GiB、MemAvailable 約7.0GiB、**swap 4GiB がほぼ満杯**
  （主にデスクトップ側のプロセス）、12 CPU、`/` NVMe ext4 767GiB 空き、MinIO と作業領域は別の HDD
  （`/mnt/minio-hdd`）。本番コンテナ16個の RSS 合計は約 2.4GiB。`vm.max_map_count=1048576`。

## Decision

**アプリは安全化済みの1行1 JSON を stdout に出し、Docker json-file（ローテーション付き）を
Fluent Bit が read-only で tail して OpenSearch の検索用副本へ送る。OpenSearch は業務判断に使わない。**

### 1. 経路

`Python logging（JSON 整形・安全化・文脈）→ stdout → Docker json-file（max-size/max-file・
labels で compose サービス名を行に付与）→ Fluent Bit（tail + 位置 DB + filesystem buffer）→
OpenSearch（write alias・明示 mapping・ingest pipeline で ingested_at）→ Dashboards`。

- 業務の経路に OpenSearch への同期 I/O を入れない。アプリが書くのは stdout だけ。
- Docker socket は渡さない。コンテナの識別は json-file の `labels`/`tag` オプションが各行に書く
  `attrs`（`com.docker.compose.service`/`project`）と、アプリ自身の `service_name` で行う。
- json-file は既定の blocking モードのまま（ローカルディスクへの追記。non-blocking の黙った欠損を避ける）。
  ディスク枯渇時はアプリの stdout 書き込みも失敗し得る。これはログ基盤ではなくホストの容量監視の対象。

### 2. ログ契約

[docs/observability/log-contract.md](../observability/log-contract.md)。語彙・型・上限は
`contracts/logging.py` が唯一の宣言元。mapping はそこから検査する。

### 3. 実装の境界（呼び出し方向）

- `contracts/logging.py`: 語彙と上限だけ。
- `infrastructure/logging/`: 整形・安全化・文脈（contextvars）・Temporal の interceptor・
  Workflow 用の replay 安全な発行関数・API middleware。`domain` は import しない（ログを出さない）。
- 発行するのは `apps`・`workers`・`infrastructure`。どこも OpenSearch を import・接続しない
  （architecture test で検査）。
- `deploy/logging/`: Fluent Bit・OpenSearch・Dashboards の設定と bootstrap。アプリの compose とは
  **別の compose project**（`avp2-logging`）にし、アプリ側の変更は `logging:` オプションと
  `AVP_SERVICE_NAME`/`AVP_ENVIRONMENT` の env だけにする。ログ基盤を止めてもアプリは変わらない。

### 4. index と保持

- 用途×環境の系統: `avp-app-<env>`（アプリ JSON）と `avp-infra-<env>`（JSON でない行）。
  書き込みは write alias `avp-app-<env>-write`、実 index は `avp-app-<env>-000001` から。
  Episode・service 別の index は作らない。
- 初期値: primary 1 / replica 0（single-node、高可用ではない）、rollover 5GiB（primary shard）
  または 7日、rollover 後 14日で削除（infra は 3日で rollover、7日で削除）。
  保持は index 単位なので、イベントごとの厳密な14日ではなく**約14〜21日＋ISM 実行遅延**。
- template・mapping（`dynamic: false`、数値・日付は `ignore_malformed`）・ISM・初期 index・alias を
  冪等に bootstrap する。alias 名の実 index 化は `action.auto_create_index` で防ぐ。

### 5. 障害時

- Fluent Bit の filesystem buffer と tail 位置 DB を専用 volume に永続化。buffer は上限付き
  （超えたら古い chunk から破棄）、retry は上限付き。**有限バッファでは無欠損と無停止を同時に保証
  できない**ので、業務を止めない側を選び、破棄・再送・滞留を監視する。
- 欠損検知は OpenSearch の外（ホストで動く確認スクリプトが Fluent Bit の metrics と OpenSearch の
  最終取り込み時刻を読む）。「ログが無い」で未実行と判断せず、DB・Temporal と照合する。

### 6. 安全

- 秘密は stdout に出す前に除去（log-contract §7）。Collector の Lua は追加防御。
- OpenSearch は security plugin の TLS・認証を有効。デモ証明書・既定ユーザーを使わない
  （自前 CA を生成するスクリプト、パスワードは repo 外）。Collector は対象 alias への書き込みだけ、
  閲覧者は読み取りだけ、管理権限は bootstrap だけ。9200/5601 は 127.0.0.1 のみ。

### 7. 資源

OpenSearch heap 1GiB・コンテナ上限 2.5GiB・swap 不使用、Dashboards 上限 1GiB、Fluent Bit 256MiB。
データは NVMe の専用 volume（MinIO の HDD と I/O を競合させない）。swap が満杯の現状では、
**本番ホストへの共存は隔離試験での実測と空きメモリの監視を条件とする**。条件を満たさなければ
`deploy/logging` をそのまま別ホストで動かし、Fluent Bit だけを本番ホストに置く構成を取る。

### 不変条件

- **INV-38** ログの発行・収集・検索基盤の障害は業務処理を失敗・停止させず、ログ検索の結果を業務判断・
  再実行・課金判定の根拠にしない。
- **INV-39** 秘密情報・provider 応答全文・prompt 全文・メディアのバイト列は stdout に出る前に除去され、
  第三者 logger と未捕捉例外も同じ整形を通る。
- **INV-40** Workflow のログ発行は決定性を崩さず（時刻・乱数・UUID・I/O を足さない）、replay で
  業務イベントを重複発行しない。

## Alternatives

- **アプリから OpenSearch へ直接送る（Python handler / OTLP）**: 業務プロセスに同期・非同期の
  ネットワーク I/O と再送バッファを持ち込み、停止時に動画処理へ影響し得る。採らない。
- **Docker の fluentd logging driver**: driver を変えると `docker logs` が使えなくなり、Collector 停止時に
  driver の挙動（blocking/non-blocking）が業務に直結する。json-file を残し tail する方が切り離せる。
- **Docker socket からメタデータを取る（docker_events / filter_docker）**: socket は read-only でも
  コンテナ作成＝ホスト制御に等しい権限。json-file の `labels` で足りる。
- **Data Prepper / Kafka / Tempo / Prometheus の新設**: 現在の量（1日数本の Episode）と要件（Logs 中心）
  では必要性が実証されていない。ingested_at は OpenSearch の ingest pipeline で足りる。
- **Elasticsearch / Loki**: 指示と既存設計（OpenSearch_Log_Design.md）に合わせ OpenSearch。Loki は
  ラベル以外の全文・フィールド検索が弱く、scene 単位の照合に向かない。
- **structlog 等のライブラリ追加**: 標準 logging の Formatter/Filter で要件を満たせ、第三者 logger も
  同じ経路で扱える。依存を増やさない。
- **Episode 別・日次 index**: shard が乱立し、single-node の heap を圧迫する。

## Consequences

良い:

- Episode・scene・attempt・provider ジョブを横断して検索でき、403/422/fallback/resume/課金予約を
  シーン単位で照合できる。
- ログ基盤はアプリと別 project で、止めても・消してもアプリの挙動は変わらない（rollback が容易）。
- json-file のローテーションで、現状の無制限なコンテナログ増加が止まる。

悪い・引き受けた負債:

- 有限バッファのため、長時間の OpenSearch 停止ではログが欠損し得る（欠損は検知して報告するが防げない）。
- 文書 ID による重複抑制は同一 index 内だけ。rollover をまたぐ再送は重複し得る。
- 保持期限は index 単位で最大約21日＋遅延。厳密な削除期限が要る用途には使えない。
- single-node / replica 0 のため、ディスク故障で検索用ログは失われる（SSoT ではないので業務は影響なし）。
- アプリの compose に `logging:` を足すと、適用にはコンテナの再作成が要る（次の deploy-workers で反映）。
- 本番ホストはメモリに余裕が少なく、共存の可否は実測に依存する。
