# staging（本番と隔離した検証環境）

Status: **Proposed**（ADR-0050）。`compose.staging.yaml` は書いてあるが、**起動は所有者の承認後**。
ここの手順を書いた時点では一度も起動していない。

## 1. 何が本番と別か

| 項目 | 本番（compose.yaml） | staging（compose.staging.yaml） |
|---|---|---|
| compose project | `avp2` | `avp2-staging` |
| Postgres | `pgdata` volume・DB `avp`・5432 | `staging_pgdata`・DB `avp_staging`・127.0.0.1:15432 |
| MinIO | `/mnt/minio-hdd/minio-data` bind・bucket `artifacts`・9000/9001 | `staging_minio` named volume・bucket `artifacts-staging`・127.0.0.1:19000/19001 |
| Temporal | namespace `default`・7233 | namespace `avp-staging`・127.0.0.1:17233（UI 18233） |
| API | 8000 | 127.0.0.1:18000 |
| イメージ | `avp2-app:local` / `avp2-worker:local` | `avp2-staging-app:local` / `avp2-staging-worker:local` |
| FAL / YouTube | 本番の資格情報 | 空。`PAUSED` / `UPLOADS_PAUSED` は true |
| 常駐 worker | 全部 | dummy / pipeline / production（状態系）/ research。画像・動画・音声・投稿・Codex 系は**定義しない** |

イメージタグを分けるのは重要: 同じタグで build すると本番コンテナが「STALE」になる
（`workers-versions.sh`）。`${...}` を使わないのは、本番の `.env` が staging に流れ込まないため。
`tests/contract/test_compose_staging.py` が上の分離を機械的に検査する。

## 2. 起動と停止（承認後）

```bash
# 起動（本番のスタックには触れない。-p/-f を必ず付ける）
docker compose -f compose.staging.yaml build --build-arg GIT_REVISION=$(git rev-parse HEAD) api pipeline-worker
docker compose -f compose.staging.yaml up -d
docker compose -f compose.staging.yaml ps
# 停止（volume は残る）。消すときは -v（staging の volume だけ）
docker compose -f compose.staging.yaml down
```

`docker compose` を `-f compose.staging.yaml` 無しで実行しない（本番を触る）。ホストの負荷が本番と競合するので、
render 中・Daily の直前は避ける。

## 3. 本番 dump の取り込み（migration 予行）

本番 DB には**書かない**。dump は読み取りだけで取り、staging の DB へ復元する。

```bash
# 取得（本番へは読み取りだけ。--no-owner で所有者依存を外す）。取得は所有者の承認後
docker compose exec -T postgres pg_dump -U "$POSTGRES_USER" -Fc --no-owner "$POSTGRES_DB" > /secure/path/avp-<date>.dump
# staging へ復元（staging の postgres コンテナ）
docker compose -f compose.staging.yaml exec -T postgres pg_restore -U avp_staging -d avp_staging --clean --if-exists --no-owner < /secure/path/avp-<date>.dump
```

dump には個人/運用データが入る。リポジトリ・ログ・共有領域に置かない（0600・repo 外）。
secret（API キー・OAuth token）は DB に無いが、`.env` の値を staging へ持ち込まない。

## 4. 検証手順

1. **migration 予行**: §3 で復元した状態に、target SHA の `alembic upgrade head` を実行する
   （`docker compose -f compose.staging.yaml run --rm migrate`）。所要時間・ロック・失敗を記録する。
   続けて `python scripts/release-preflight.py` の `alembic` / `migration_compat` と同じ判定を手で再確認する
2. **API**: `curl -s http://127.0.0.1:18000/healthz` 等（実エンドポイントは `apps/api`）と、Episode 一覧が復元データで返る
3. **UI**（ある場合）: UI の向き先を `http://127.0.0.1:18000` にして読み取り操作のみ確認。投稿・生成の操作はしない
4. **worker 再起動**: `docker compose -f compose.staging.yaml restart pipeline-worker production-worker` 後、
   healthcheck が healthy に戻り、`TEMPORAL_ADDRESS=127.0.0.1:17233 TEMPORAL_NAMESPACE=avp-staging` で
   `python -m infrastructure.temporal.poller_check --queue pipeline --queue production` が 0
5. **版**: staging のコンテナの `org.opencontainers.image.revision` ラベルが全て target と一致

## 5. replay（決定性の証跡）

本番の履歴を staging へ持ち込まず、repo の履歴 fixture（`tests/unit/fixtures` の workflow history）を
target SHA のコードで replay する。実行は fake のみ（有料 API・YouTube なし）:

```bash
pytest tests/unit/test_log_old_history_replay.py tests/unit/test_pipeline_workflows.py -q  # 全て pass なら証跡を書く
```

pass したら `release/evidence/<sha>-replay.json`（`{"sha","result":"pass","at"}`）を置く。失敗は `"fail"`。
本番の実 workflow の履歴を使う replay は、`temporal workflow show --output json` の読み取り結果を
staging 側 worker のコードで Replayer に通す形になる（未実装・要設計）。

## 6. やらないこと

- staging から FAL / YouTube / 本番 Temporal / 本番 MinIO に接続する（資格情報が空・ネットワークが別）
- staging の volume を本番の復旧に使う（復旧は release-runbook §5 の手順と所有者の判断）
- staging の結果を「本番で再現した」と同一視する（provider を通る経路は fake でしか試せない）
