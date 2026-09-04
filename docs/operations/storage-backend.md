# MinIO のストレージ backend（**未解決の重大問題**）

Status: **Phase 1 の固定を阻む問題。所有者の判断が要る。** (2026-09-04)

## 症状

`compose.yaml` は MinIO のデータを `/mnt/minio-hdd/minio-data` に
bind mount している。この構成では:

- `put_object` は**成功を返す**
- しかし直後の `list_objects` は**空**を返す
- `get_object` / `stat_object` は **`AccessDenied`** を返す
- ディスク上には `xl.meta` が実際に書かれている（データは存在する）

つまり **MinIO が「書けたと言うが、読み戻せない」** 状態になる。

## 実測（2026-09-04）

```text
/mnt/minio-hdd:  /dev/sda1  fuseblk  1.9T   ← FUSE 経由のブロックデバイス（NTFS等）
```

同じ MinIO イメージ・同じクライアントコードで、保存先だけを変えた対照実験:

| データ保存先 | PUT | LIST | GET | HEAD |
|---|---|---|---|---|
| Docker named volume（overlay/ext4） | OK | OK | OK | OK |
| `/mnt/minio-hdd/minio-data`（fuseblk bind mount） | OK | **空** | **AccessDenied** | **AccessDenied** |

MinIO のログにも同種のエラーが出ている:
`Prefix access is denied: .minio.sys/buckets/.usage-cache.bin (cmd.PrefixAccessDenied)`

MinIO は POSIX セマンティクス（アトミックな rename、正しいディレクトリ走査など）を
前提としており、FUSE 経由の非POSIXファイルシステム（NTFS / exFAT / CIFS など）は
サポート対象外である。これはアプリケーション側のバグではない。

## 影響

**INV-9（MinIOがArtifact本体を保持する）が、この構成では実質的に破れている。**

- `./scripts/smoke.sh` は Episode が `completed` になれば緑になっていた。
  しかし保存された Artifact は読み戻せなかった。**偽の緑**だった
- `produce_dummy_artifact` は書き込み前に `_get_bytes()` で存在確認するが、
  存在しないキーは（浅い階層では）`NoSuchKey` を返すため素通りし、
  put が「成功」するので、ワークフローは最後まで完走してしまう
- Phase 2 で「途中再開」（出力Artifactが既にあれば工程をskip）を実装すると、
  読み戻せない＝毎回作り直しになり、**有料工程で二重課金**につながる

## 検出方法（既に入っている）

- `tests/integration/test_minio_store.py` — 実MinIOに対する put→get→exists の
  往復テスト。この構成では**正しく失敗する**（テストが正しく、環境が壊れている）
- `./scripts/smoke.sh` — Episode 完了後に Artifact を読み戻し、sha256 を照合する
  ステップを追加した。「書けたが読めない」を smoke が見逃さないようにした

## 選択肢（所有者の判断）

1. **Docker named volume を使う**（推奨・最小）
   `compose.yaml` の `minio` の volumes を `miniodata:/data` に戻す。
   開発環境としては最も確実。ただし 1.9T の HDD 容量は使えない。
2. **HDD を POSIX ファイルシステムで再フォーマットする**（ext4 / xfs）
   容量を活かしたまま MinIO を正しく動かせる唯一の方法。**ディスクの内容は消える**。
3. **HDD 上に ext4 のディスクイメージを置き、loop mount する**
   再フォーマットせずに POSIX セマンティクスを得る。性能と運用の複雑さは増す。
4. **オブジェクトストレージを MinIO 以外にする**
   ADR-0003 の見直しになる。Phase 1 の範囲を超える。

いずれを選んでも `compose.yaml` と ADR-0003 の更新を伴うため、
**決定するまで Phase 1 を「安全な基準点」として固定してはならない。**

## 陳腐化条件

- 保存先を変更したとき → 本文書を更新し、上記の対照実験をやり直す
- MinIO が非POSIXファイルシステムを正式サポートしたとき（現状その予定は無い）
