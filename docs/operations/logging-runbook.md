# ログ基盤 runbook（草案）

ADR-0040。収集・検索側の設定の詳細は [platform.md](../observability/platform.md)、発行側は
[emission-points.md](../observability/emission-points.md)。名前・コマンドは 2026-10-06 の隔離試験
（[verification-results.md](../observability/verification-results.md)）で実際に使ったもの。

> **ログは検索用の副本。** 業務状態・課金・冪等性は PostgreSQL、実行履歴は Temporal が正（INV-7/8）。
> ログ検索の結果で課金済みか・再実行・再開・投稿を判断しない（INV-38）。
> 「ログが無い」は未実行・収集停止・バッファ待ち・rotation 欠損を区別できない。

## 1. 構成の要点

- アプリ（compose project `avp2`）は stdout に1行1 JSON を出すだけ。Docker json-file
  （20m × 5、labels）に残る。
- ログ基盤は**別の compose project** `avp2-logging`: Fluent Bit（containers/ を read-only で tail、
  位置 DB・filesystem buffer）→ OpenSearch（TLS・認証、write alias）→ Dashboards（使う時だけ）。
- ポート 9200・5601 は 127.0.0.1 のみ。Fluent Bit の 2020 は公開せず `scripts/fb-metrics.sh` で読む。別マシンから見るときは SSH トンネル
  （`ssh -L 5601:127.0.0.1:5601 <host>`）。
- 資格情報は `~/.config/avp-logging/<env>/`（0700、各ファイル 0600。platform.md §4）。env やコマンド引数に書かない。
  alias・ISM の状態は viewer では読めない（403）。読むときは admin 証明書（`pki/admin.{pem,key}`）で GET だけ。

## 2. 起動・停止・bootstrap

```bash
C="docker compose -p avp2-logging -f deploy/logging/compose.logging.yaml"
# 初回だけ: 秘密（~/.config/avp-logging/prod、0600）と証明書・internal_users
deploy/logging/scripts/init-secrets.sh --env prod
$C run --rm security-init
# 起動
$C up -d --wait opensearch
# bootstrap（冪等。auto_create の確認 → alias 誤作成の検出 → ingest pipeline → component template
#  → index template → ISM policy → 初期 index + write alias）。何度流しても同じ状態（実測: 2回目は unchanged）
$C run --rm bootstrap
$C up -d fluent-bit                       # 初回は §8 の二段構え
# Dashboards（使う時だけ。メモリ 1GiB 上限）
$C --profile dashboards-setup run --rm dashboards-keystore    # 初回
$C --profile dashboards up -d dashboards
$C --profile dashboards-setup run --rm dashboards-import      # saved objects（初回・更新時）
# 状態（非0 なら異常。journal にも残る。§6 の注意: Fluent Bit の停止（stall）は lag でしか見えない）
deploy/logging/scripts/check-pipeline.sh --env prod --project avp2-logging
deploy/logging/scripts/fb-metrics.sh --project avp2-logging /api/v1/storage
# 停止（アプリには影響しない。Fluent Bit は位置 DB から再開できる）
$C --profile dashboards stop
```

- 長く止めるときは OpenSearch だけでなく **Fluent Bit も止める**（retry 上限 約6時間を超えると
  buffer の record が破棄される。止めておけば rotation が一巡するまでは欠損しない）。
- `down -v` は検索用ログを全て消す（業務には影響しない）。本番で実行するのは所有者の判断だけ。

### メモリ閾値（ADR-0040 §7）

| MemAvailable | 対応 |
|---|---|
| < 2 GiB | Dashboards を止める |
| < 1.5 GiB | OpenSearch を止める（Fluent Bit は buffer に溜め、上限で破棄） |

`check-pipeline.sh --enforce-memory`（systemd timer）は `avp2-logging` のコンテナ**だけ**を止める。

## 3. Dashboards の見方

1. `http://127.0.0.1:5601`（Dashboards 自体は TLS なし。127.0.0.1 のみ公開なので SSH トンネル経由）に `avp_viewer`（`~/.config/avp-logging/prod/viewer.pw`）でログイン。
2. Discover で index pattern `avp-app-prod-*`（インフラは `avp-infra-prod-*`）。時刻フィールドは
   `@timestamp`（発生時刻）。取り込み遅れは `ingested_at` との差で見る。
3. 保存済み検索（`deploy/logging/dashboards/saved-objects.ndjson`）: 「AVP: Episode 時系列」
   「AVP: scene/revision 別 403・422・fallback」「AVP: fallback・resume」「AVP: blocked」
   「AVP: Render・Upload の失敗」「AVP: service と git_sha」「AVP: infra（unstructured）」
   「AVP: Collector の修復・型不整合」。index pattern は `avp-app-*` / `avp-infra-*`。
4. 並びは時刻だけで因果を断定しない。`activity_attempt`・`provider_attempt`・`scene_revision`・
   `run_id` を併せて見る。

## 4. Episode の調査手順

1. **ログ**: `episode_id:"<id>"` で時系列。`stage.*` で止まった工程、`scene.rejected`・
   `provider.call.failed`（`http_status`・`error_code`・`error_category` + `classification_basis`）で
   シーンと原因の候補。`provider_request_id`・`reservation_id` を控える。
2. **DB（正）** で確定する:

   ```sql
   SELECT id, status, updated_at FROM episodes WHERE id = '<id>';
   SELECT id, job_type, status, attempts, error_summary, updated_at FROM jobs
     WHERE episode_id = '<id>' ORDER BY created_at;
   SELECT id, provider, scene_id, round, status, dispatched_at, reconciled_by, provider_job_ref
     FROM provider_reservations WHERE episode_id = '<id>' ORDER BY created_at;
   SELECT scene_id, category, types, http_status, created_at FROM provider_rejections
     WHERE episode_id = '<id>' ORDER BY created_at;
   SELECT provider, http_status, created_at FROM provider_auth_incidents ORDER BY created_at DESC LIMIT 20;
   SELECT id, artifact_type, scene_id, version, superseded_at FROM artifact_metadata
     WHERE episode_id = '<id>' ORDER BY created_at;
   ```
   （列名は実スキーマで確認する。`psql` は `docker compose exec postgres psql -U avp avp`、読み取りのみ）
3. **Temporal**: ログの `workflow_id`・`run_id` で `temporal workflow show` / UI（8233）の履歴を見る。
   `completed` は成功を意味しない（ADR-0031）。
4. 予約が `reserved` のまま・`dispatched_at` あり・job ref 無し → 未照合。provider 側で確認して確定する
   まで**再送しない**（AGENTS §9）。ログの `provider.call.succeeded`（submit、ref の commit 前に出る）は
   provider のジョブを辿る手がかりで、課金確定ではない。
5. **「日次のログが無い」だけで未実行と断定しない。** 順に確認: `daily_episode_slots` と `episodes`
   の当日行 → Temporal の Schedule と pipeline workflow → `check-pipeline.sh`（Collector 停止・
   buffer 待ち・取り込み遅れ）→ `docker logs`（json-file にはまだ残っている）。

## 5. 検索例

Dashboards（DQL）:

```
episode_id:"<id>" and event_name:scene.*
episode_id:"<id>" and scene_id:"sb6" and (http_status:403 or http_status:422)
event_name:provider.call.failed and error_category:access_denied
event_name:reservation.* and reservation_status:reserved
level:ERROR and not service_name:"api"
collector_errors:*                       # Collector が型を退避した行
```

curl（読み取りユーザー。パスワードはファイルから）:

```bash
S=~/.config/avp-logging/prod; OS=https://127.0.0.1:9200
# パスワードを argv に出さない（ps で見える）: curl の設定をプロセス置換で渡す
q() { curl -sS --cacert "$S/pki/ca.pem" -K <(printf 'user = "avp_viewer:%s"\n' "$(cat "$S/viewer.pw")") \
        -H 'Content-Type: application/json' "$@"; }
q \
  "$OS/avp-app-prod-*/_search" -d '{
    "size": 200, "sort": [{"@timestamp": "asc"}],
    "query": {"bool": {"filter": [{"term": {"episode_id": "<id>"}}]}},
    "_source": ["@timestamp","event_name","stage","scene_id","provider_attempt","http_status","error_code","reservation_id","message"]}'
```

`event_id` での重複除去（rollover をまたぐ再送は別文書になり得る）:

```bash
q "$OS/avp-app-prod-*/_search" -d '{
    "size": 200, "query": {"term": {"episode_id": "<id>"}},
    "collapse": {"field": "event_id"}, "sort": [{"@timestamp": "asc"}]}'
# 重複している event_id の数
q "$OS/avp-app-prod-*/_search" -d '{"size":0,"aggs":{"d":{"terms":{"field":"event_id","min_doc_count":2}}}}'
```

件数の assert・集計は `deploy/logging/test/search-assert.py`（`OPENSEARCH_*` env を設定）でも行える。
例: `search-assert.py agg --term episode_id=<id> --by event_name,scene_id --table`。

## 6. Collector の破棄・再送・lag の確認

- `check-pipeline.sh` が読むもの: Fluent Bit metrics（`127.0.0.1:2020/api/v2/metrics/prometheus`）の
  `retries`・`retries_failed`・`dropped_records`・storage の chunk 数、OpenSearch の最新 `ingested_at`、
  証明書の期限、ディスク、MemAvailable。
- 手で見る:

  ```bash
  curl -s 127.0.0.1:2020/api/v2/metrics/prometheus | grep -E 'retr|drop|storage|skip'
  deploy/logging/test/search-assert.py lag --max-seconds 300
  ```
- `dropped_records_total` / `retries_failed_total` が増えた = その分の検索用ログは失われた
  （業務には影響しない）。原因（OpenSearch 停止・400 の型不整合）を直し、欠けた時間帯は
  json-file（rotation 前なら）か DB・Temporal で補う。
- `files_opened_total == 0` が続く = Collector が何も読めていない（権限・mount 不成立）。
- **stall**（2026-10-06 の隔離試験で再現。review-log の I-15）: Docker が partial に分けた
  1行が結合後に `buffer_max_size`（256k）を超えると、tail が CPU 100% のまま全ファイルの読み取りを
  止める。health は ok・`long_line_skipped` は 0・buffer chunk も増えない。見えるのは最終
  `ingested_at` の lag と `input_records_total{name="tail.0"}` が増えないこと、`docker stats` の CPU。
  対処: `docker restart avp2-logging-fluent-bit-1`（位置 DB から再開。その巨大行を含むファイルの
  残りは読まれず失われる）。B の修正までは lag の監視を短め（`--max-lag-min`）にする。

## 7. 欠損時の復旧・deploy 前の確認

- **deploy-workers の前に** Collector の追いつきを確認する（コンテナ再作成で未読の json-file が
  消えるため）: 位置 DB の offset と各 `*-json.log` のサイズの差が 0 に近いこと。
  `check-pipeline.sh` はこの差を見ない（buffer の chunk 数と最終 `ingested_at` だけ。
  review-log の I-17）。当面は試験用の `deploy/logging/test/catchup.py` を使う:

  ```bash
  LOGGING_PROJECT=avp2-logging AVP_LOG_TARGET_PROJECT=avp2 \
    bash -c 'source deploy/logging/test/lib.sh && catchup'      # {"ok": true, "behind_bytes": 0, ...}
  ```
  （位置 DB を WAL ごとホストの一時ディレクトリへ複製して読む。Fluent Bit には触れない）
  追いついていなければ待つ。`behind_bytes` が減らないときは §6 の stall を疑う。
- Collector 停止が長く、rotation 一巡（20m × 5 ÷ 出力速度）を超えた分は取り戻せない（検知もできない）。
  その時間帯は DB・Temporal を正として調べる。
- 位置 DB を消すと、残っている json-file を先頭から読み直す。同じ index 内なら `event_id` で重複は
  抑止されるが、rollover 済みなら重複する。消す前に所有者の判断を取る。

## 8. 導入（read_from_head 二段構え）と rollback

導入:

1. `avp2-logging` を起動し bootstrap。**初回だけ** Fluent Bit を `read_from_head: false` で起動し、
   位置 DB を作る（既存の大きな json-file を全量取り込まない）。
2. 位置 DB ができたら `read_from_head: true` に戻して再起動（以後、Collector 停止中の追記・rotation を
   読み漏らさない）。
3. アプリの compose に `logging:`・env（`AVP_SERVICE_NAME`・`AVP_ENVIRONMENT`・`AVP_LOG_FORMAT`）を足し、
   次の `make deploy-workers` で反映（コンテナ再作成が要る）。deploy 前に §7 の追いつき確認。

rollback（軽い順。どの段でもアプリの業務は止めない）:

1. ログ基盤を止める: `docker compose -p avp2-logging … stop`（アプリは json-file に書き続ける）。
2. アプリの出力を従来形式へ: `AVP_LOG_FORMAT=text` で deploy（安全化は text でも行われる）。
3. アプリの compose の `logging:` を戻す（rotation 無しへ戻る。容量の増加に注意）。
   API の起動コマンドを戻すのは 2 で足りない場合だけ。

## 9. 隔離環境での試験

本番では障害注入をしない。手順は [docs/observability/verification-plan.md](../observability/verification-plan.md)、
部品は `deploy/logging/test/`（隔離 app スタック `avp2-oslog-c*`）。
