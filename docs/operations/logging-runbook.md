# ログ基盤 runbook（草案）

ADR-0040。**草案**: B（`deploy/logging/`）・A（`infrastructure/logging`）の成果で確定した名前
（compose ファイル・サービス名・スクリプト名・ポート）に合わせてフェーズ2で更新する。
ここに `<…>` で書いた箇所は未確定。

> **ログは検索用の副本。** 業務状態・課金・冪等性は PostgreSQL、実行履歴は Temporal が正（INV-7/8）。
> ログ検索の結果で課金済みか・再実行・再開・投稿を判断しない（INV-38）。
> 「ログが無い」は未実行・収集停止・バッファ待ち・rotation 欠損を区別できない。

## 1. 構成の要点

- アプリ（compose project `avp2`）は stdout に1行1 JSON を出すだけ。Docker json-file
  （20m × 5、labels）に残る。
- ログ基盤は**別の compose project** `avp2-logging`: Fluent Bit（containers/ を read-only で tail、
  位置 DB・filesystem buffer）→ OpenSearch（TLS・認証、write alias）→ Dashboards（使う時だけ）。
- ポート 9200・5601・2020 は 127.0.0.1 のみ。別マシンから見るときは SSH トンネル
  （`ssh -L 5601:127.0.0.1:5601 <host>`）。
- 資格情報は `~/.config/avp-logging/`（0600）。env やコマンド引数に書かない。

## 2. 起動・停止・bootstrap

```bash
# 起動（Dashboards は profile で別。メモリが厳しい時は起動しない）
docker compose -p avp2-logging -f deploy/logging/compose.logging.yaml up -d            # <B で確定>
docker compose -p avp2-logging -f deploy/logging/compose.logging.yaml --profile dashboards up -d dashboards
# bootstrap（冪等。component template → index template → ISM policy → ingest pipeline → 初期 index + alias）
<deploy/logging/scripts/bootstrap.sh>
# 状態
deploy/logging/scripts/check-pipeline.sh            # 非0 なら異常（journal にも残る）
# 停止（アプリには影響しない。Fluent Bit は位置 DB から再開できる）
docker compose -p avp2-logging -f deploy/logging/compose.logging.yaml stop
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

1. `http://127.0.0.1:5601`（トンネル経由）に `avp_log_viewer` でログイン。
2. Discover で index pattern `avp-app-prod-*`（インフラは `avp-infra-prod-*`）。時刻フィールドは
   `@timestamp`（発生時刻）。取り込み遅れは `ingested_at` との差で見る。
3. 保存済み検索（<B で確定した名前>）: Episode 時系列 / scene 別の 403・422・fallback /
   service_name × git_sha（版混在）/ blocked・Render 失敗・Upload 失敗 / Collector の状態。
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
OS=https://127.0.0.1:9200; CA=~/.config/avp-logging/ca.crt
AUTH="avp_log_viewer:$(cat ~/.config/avp-logging/viewer.password)"
curl -sS --cacert "$CA" -u "$AUTH" -H 'Content-Type: application/json' \
  "$OS/avp-app-prod-*/_search" -d '{
    "size": 200, "sort": [{"@timestamp": "asc"}],
    "query": {"bool": {"filter": [{"term": {"episode_id": "<id>"}}]}},
    "_source": ["@timestamp","event_name","stage","scene_id","provider_attempt","http_status","error_code","reservation_id","message"]}'
```

`event_id` での重複除去（rollover をまたぐ再送は別文書になり得る）:

```bash
curl -sS --cacert "$CA" -u "$AUTH" -H 'Content-Type: application/json' \
  "$OS/avp-app-prod-*/_search" -d '{
    "size": 200, "query": {"term": {"episode_id": "<id>"}},
    "collapse": {"field": "event_id"}, "sort": [{"@timestamp": "asc"}]}'
# 重複している event_id の数
curl ... -d '{"size":0,"aggs":{"d":{"terms":{"field":"event_id","min_doc_count":2}}}}'
```

件数の assert は `deploy/logging/test/search-assert.py`（`OPENSEARCH_*` env を設定）でも行える。

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

## 7. 欠損時の復旧・deploy 前の確認

- **deploy-workers の前に** Collector の追いつきを確認する（コンテナ再作成で未読の json-file が
  消えるため）: 位置 DB の offset と各 `*-json.log` のサイズの差が 0 に近いこと
  （`<check-pipeline.sh --catchup>`、B で確定）。追いついていなければ待つ。
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
