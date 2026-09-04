# MinIO のストレージ backend

Status: **Resolved (2026-09-04)** — HDD を ext4 で再フォーマットして解消。
`/mnt/minio-hdd` は現在 `/dev/sda1 ext4`。

この文書は解決済みの記録であると同時に、**保存先を変えるときの制約**を定める。

## 制約（現在も有効）

**MinIO のデータ保存先は POSIX セマンティクスを持つファイルシステムでなければならない。**
NTFS / exFAT / CIFS などの FUSE マウント（`fuseblk`）は MinIO のサポート対象外であり、
使うと**書き込みは成功するのに読み戻せない**状態になる。

`compose.yaml` の `minio` サービスは `/mnt/minio-hdd/minio-data` を bind mount しており、
この制約はそのホストパスに掛かる。

## 経緯（2026-09-04 に発生し、同日解消）

当初 `/mnt/minio-hdd` は `fuseblk`（FUSE 経由のブロックデバイス）でマウントされていた。
この構成で次が起きた:

- `put_object` は**成功を返す**
- しかし直後の `list_objects` は**空**を返す
- `get_object` / `stat_object` は **`AccessDenied`** を返す
- ディスク上には `xl.meta` が実際に書かれていた（データは存在するが MinIO が配れない）
- さらに MinIO を再起動すると **`FATAL Unable to initialize backend`** で起動不能になった

MinIO のログにも同種のエラーが出ていた:
`Prefix access is denied: .minio.sys/buckets/.usage-cache.bin (cmd.PrefixAccessDenied)`

## 対照実験

同じ MinIO イメージ・同じクライアントコードで、保存先だけを変えた実測。

| 保存先 | 実測日 | PUT | LIST | GET | HEAD |
|---|---|---|---|---|---|
| Docker named volume（overlay/ext4） | 2026-09-04 | OK | OK | OK | OK |
| `/mnt/minio-hdd/minio-data`（**fuseblk**） | 2026-09-04 | OK | **空** | **AccessDenied** | **AccessDenied** |
| `/mnt/minio-hdd/minio-data`（**ext4**、現行） | 2026-09-04 | OK | OK (1) | OK (90B) | OK |

現行構成では全操作が成功する。`./scripts/smoke.sh` も Artifact の読み戻しと
sha256 照合まで通っている。

## この事故が教えたこと

**「書けた」だけでは INV-9 を満たさない。**
`produce_dummy_artifact` は書き込み前に存在確認するだけで読み戻さないため、
成果物が失われていてもワークフローは完走し、smoke は緑になっていた。
Phase 2 の「途中再開」（出力Artifactが既にあれば工程をskip）を載せると、
読み戻せない＝毎回作り直しになり、**有料工程の二重課金**に直結していた。

そのため、再発を検出する機械を2つ入れてある:

- `tests/integration/test_minio_store.py` — 実MinIOに対する put → get → exists の往復
- `scripts/smoke.sh` — Episode 完了後に Artifact を読み戻し、sha256 を照合する。
  「書けたが読めない」を smoke が緑にしない

## 保存先を変更するときの手順

1. 変更先が POSIX ファイルシステム（ext4 / xfs 等）であることを `findmnt -no FSTYPE <path>` で確認
2. `compose.yaml` の `minio.volumes` を更新
3. 上の対照実験（PUT / LIST / GET / HEAD）をやり直し、本文書の表に行を追加
4. `./scripts/smoke.sh` が読み戻しまで通ることを確認

## 陳腐化条件

- 保存先を変更したとき → 上記手順に従い本文書を更新する
- オブジェクトストレージを MinIO 以外にしたとき → ADR-0003 の見直しとあわせて本文書を廃止する
