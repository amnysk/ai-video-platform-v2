# 判断記録と介入: 運用手順（設定・互換・復旧・backup/restore）

JP-MAS-001 v2.0 / [ADR-0041](../decisions/0041-decision-records-and-intervention.md)。契約は
[docs/domain/intervention.md](../domain/intervention.md)、HTTP の使い方は [intervention-api.md](./intervention-api.md)。
この文書は担当 D の範囲（ログ投影・再投影・整合検査・backup/restore）を中心に、運用者が触る手順を書く。

> **状態（2026-10-07）**: 担当 B（command の適用・dispatch gate・再配送 Schedule）は統合 branch に未 merge。
> §4 の「未配送 command の復旧」は B の実装の前提を書いたもので、実機で検証していない。

## 1. 設定

| キー | 既定 | 意味 |
|---|---|---|
| `INTERVENTION_PRINCIPALS` | 未設定（=介入 API 全体が `503 intervention_disabled`） | token の sha256 → principal（actor・scopes）。[intervention-api.md](./intervention-api.md) §1 |
| `EPISODE_BUDGET_JPY` / `MONTHLY_BUDGET_JPY` / `BUDGET_USD_JPY_RATE` | 1000 / 30000 / なし | （2026-10-07 時点で Settings に未実装。担当 B が足す）新方式の ChangeSet 適用時の予算検査（[intervention.md](../domain/intervention.md) §9）。**介入 API・AI からは変更できない**（INV-47） |
| `AVP_LOG_FORMAT` 等 | `json` | 判断・介入イベントの投影も既存のログ設定に従う（[log-contract.md](../observability/log-contract.md) §10） |

新しい表（migration 0016）は**新しい表だけ**を足す。既存の表・列・遷移表は変えない。

## 2. 旧 Episode・日次実行との互換

- `episode_controls` に行が無い Episode は `legacy` + `running` として読む（行を作らない）。日次 Schedule の入力・既定は
  変えないので、日次 Episode は `legacy` のまま従来どおり動く。
- 台本の「結び直し」（`revision_links(kind=rebound_to)`）が無い Episode では、production / voice / render /
  scene_recovery は従来どおり `storyboard.source_script` を使う（`infrastructure/intervention/rebind.py`）。
- 判断・介入のログ（`decision.recorded`、`intervention.*` 等）は新しい `event_name` を足しただけで、既存のイベント・
  mapping の型は変えていない（追加のみ。`LOG_SCHEMA_VERSION` は据え置き）。
- migration 0016 の巻き戻しは新しい表を消すだけ（既存データは触らない）。**消すと判断・介入の正本が失われる**ので、
  巻き戻す前に §5 の backup を取る。

## 3. ログの投影が欠けた・OpenSearch が止まった（再投影）

正本は PostgreSQL の `episode_events`。OpenSearch の停止・Collector の欠損で検索に出ない期間があっても、正本は失われない
（INV-38: アプリは OpenSearch を読まない）。

1. 正本の件数を見る（読み取りだけ）:
   `SELECT count(*) FILTER (WHERE projected_at IS NULL), count(*) FROM episode_events;`
2. OpenSearch 側の件数（`event_name: intervention.* or decision.* or review.* or checkpoint.* or agent.run.*`）と
   人が突き合わせる。
3. 再投影する。**`avp.logging=app` label の付いた新しいコンテナ**で走らせる（`docker exec` の stdout はコンテナの
   ログにならないので Collector が拾わない）:
   ```bash
   docker compose -p avp2 run --rm --no-deps api python scripts/intervention-reproject.py            # 未投影だけ
   docker compose -p avp2 run --rm --no-deps api python scripts/intervention-reproject.py --all      # 全部
   docker compose -p avp2 run --rm --no-deps api python scripts/intervention-reproject.py \
       --episode <episode_id> --from-seq 10 --to-seq 40
   ```
   `--dry-run` で件数だけ見られる。同じ行は同じ `event_id`（`uuid5(episode_id:seq)`）で出るので、何度走らせても
   同じ index の中では重複しない（日付の index をまたぐと別文書になり得る。検索側は `event_id` で重複を除く）。
4. `projected_at` は「再投影の処理が stdout へ渡した」時刻で、索引されたことの証明ではない。

## 4. 未配送・停滞した command（担当 B の実装が前提。未検証）

- command 行（`intervention_commands`）が outbox。`dispatched_at IS NULL`、または `received` / `validated` /
  `applying` のまま `REDELIVERY_STALE_AFTER_SECONDS`（120秒）を超えた行を、Schedule `avp-intervention-redelivery` が
  `InterventionCommandWorkflow(id=intervention-{command_id})` で再 start する。同じ id の重複 start は Temporal が束ね、
  終端の command は保存済み result を返す（INV-42）。
- 手で確かめる: `SELECT id, status, dispatched_at, dispatch_attempts, received_at FROM intervention_commands
  WHERE status IN ('received','validated','applying') ORDER BY received_at;`
- **成否不明の provider 予約は自動で再送・解放しない**（INV-15）。HELD の応答の `unknown_outcome` は既存の照合手順で扱う。

## 5. backup / restore と整合検査

### 5.1 backup（取得）

DB とオブジェクトストアを**同じ時点の組**として取る。順序は **DB → MinIO**（DB に載った参照のオブジェクトは
既に MinIO にある＝書き込みはオブジェクト → 行の順なので、後から取った MinIO は DB の参照を全て含む。逆順だと
DB にだけある参照が欠ける）。

```bash
pg_dump -U avp -Fc avp > avp-$(date -u +%Y%m%dT%H%M%SZ).dump
mc mirror <alias>/artifacts <backup-alias>/artifacts-<UTC時刻>
```

### 5.2 restore（復元）と検査

1. 新しい DB へ: `createdb avp_restore && pg_restore -d avp_restore --no-owner --exit-on-error <dump>`
2. オブジェクトを新しい bucket へ: `mc mb <alias>/artifacts-restore && mc mirror <backup> <alias>/artifacts-restore`
3. 復元先へ向けて整合検査（読み取りだけ。何も直さない）:
   ```bash
   DATABASE_URL=postgresql+psycopg://…/avp_restore MINIO_BUCKET=artifacts-restore \
     python scripts/consistency-check.py            # 媒体本体も検査（大きい動画は時間がかかる）
     python scripts/consistency-check.py --skip-media
   ```
   - 終了コード 0 = 整合。1 = 不整合（1件1行: `kind  table  row_id  detail`）。
   - `missing_object` / `sha256_mismatch`: MinIO の複製漏れ・別内容。backup の取り直しか、別の世代の bucket から補う。
   - `dangling_ref` / `episode_mismatch`: DB の一部だけが復元された・壊れた参照。**自動で直さない**。人が原因を調べる。
   - `artifact_metadata.bucket` 列は見ない（復元先の bucket 名が違ってもよい）。
4. 整合を確かめてから、アプリの接続先を切り替える（切り替えの手順は既存の運用に従う。この文書の範囲外）。

### 5.3 演習（隔離スタック専用）

`scripts/intervention-backup-drill.sh` が §5.1・§5.2 を隔離スタック（`deploy/logging/test/compose.apptest.yaml`、
project `avp2-oslog-c*`。本番 `avp2` は拒否）で通しで行う: 元データの投入 → `pg_dump` → 新 DB へ `pg_restore` →
`mc mirror` → 整合検査が 0 → 復元先から1オブジェクト消して検査が 1 → 後片付け。

```bash
export AVP_APPTEST_PROJECT=avp2-oslog-c-jpmasd \
       APPTEST_WORKER_IMAGE=avp2-oslog-c-jpmasd-worker:test \
       APPTEST_RUNNER_IMAGE=avp2-oslog-c-jpmasd-runner:test
deploy/logging/test/run-e2e.sh build      # 約2分（初回）
deploy/logging/test/run-e2e.sh up         # 約15秒（migration 0016 まで）
scripts/intervention-backup-drill.sh      # 約9秒。--keep で復元先を残す
deploy/logging/test/run-e2e.sh stop       # volume は残す（down で全部消す）
```

実測（2026-10-07、Episode 3本ぶん・オブジェクト54個）: dump 92KB、復元・mirror・検査まで 9 秒。検査は
objects=54・refs=72 を見て 0、1オブジェクト削除後は `missing_object` 1件で 1。
