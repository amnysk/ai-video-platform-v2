# CI の MinIO image（ADR-0046）

- 定義: `compose.ci.yaml`（CI 専用。本番・ローカルでは読まれない）
- 更新手順（月次、または CI が image 取得で落ちたとき）:
  1. `docker pull cgr.dev/chainguard/minio:latest` → `docker inspect --format '{{index .RepoDigests 0}}'`
  2. 使い捨て data dir で `server /data` を起動し、bucket 作成 / put / get / delete と
     `mc ready local` が通ることを確認
  3. `compose.ci.yaml` の digest を差し替える（検査: `tests/contract/test_compose_ci_minio.py`）
- 本番の `/mnt/minio-hdd/minio-data` には絶対に向けない。
- 本番 image の恒久策は未決（ADR-0046 Consequences）。
