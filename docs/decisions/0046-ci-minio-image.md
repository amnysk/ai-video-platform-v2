# ADR-0046: CI の MinIO image を Chainguard（digest 固定）に差し替える

## Status

Accepted

## Context

PR #3/#4 の Integration tests が `docker compose --profile core up` で落ちた:
`pull access denied for minio/minio ... denied: requested access to the resource is denied`。
2026-10-16 の実測では `minio/minio`（任意タグ・digest 指定とも）、`quay.io/minio/minio`、
`ghcr.io/minio/minio` はすべて匿名 pull 不可。レート制限ではなくリポジトリへのアクセス拒否で、
Docker Hub ログインを足しても直らない。upstream の minio/minio は source のみ配布で、
公式バイナリ image は公開されなくなった。

本番ホストの `avp2-minio-1` は RELEASE.2025-09-07T16-13-09Z の image をローカルに保持して
動いており影響なし。ただしイメージを持たない環境（新ホスト・prune 後・CI）では起動できない
（`docs/operations/workers.md` §11 に既出）。

## Decision

**CI だけ** `compose.ci.yaml` で minio を `cgr.dev/chainguard/minio@sha256:<digest>`（`user: "0"`）に
差し替え、integration job は `COMPOSE_FILE=compose.yaml:compose.ci.yaml` で読む。
本番の `compose.yaml` と `/mnt/minio-hdd/minio-data` は変更しない。

## Alternatives

- **bitnamilegacy/minio**: pull 可だが 2025-08 以降更新が止まり、セキュリティ更新が来ない。不採用。
- **source build（go install @RELEASE.2025-09-07）**: 本番と同一 version にできるが、
  archived repo と Go proxy への依存、`mc` なし、CI のビルド時間増。fallback として保持。
- **ghcr.io/amnysk へ本番 image をミラー**: 同一 bits を保てるが、registry への公開操作で
  所有者の判断が要るため本タスクでは実施しない（負債に記録）。
- **Docker Hub ログイン追加**: 原因が認証ではないため効かない。

## Consequences

- 良い: CI が再現的に起動する。S3 操作（bucket 作成・put/get/delete）、`mc ready local`
  healthcheck、2025-09 版が書いた data dir の読み出しをコピー上で確認済み。
- 悪い/負債:
  - Chainguard は `latest` タグしか匿名公開しないため digest 固定のみ。digest が GC される
    と CI が落ちる → 更新手順は `docs/operations/ci-minio-image.md`。
  - CI は本番と違う build（Chainguard fork、2026-09 版）を使う。本番との version 差は残る。
  - 本番 image の恒久策（バックアップ付き移行 or ミラー）は**別タスク**。移行時は data dir の
    バックアップとロールバック手順を先に用意すること。
