# CI MinIO override のテスト根拠（ADR-0046）

`tests/contract/test_compose_ci_minio.py`

- digest 固定: タグだけだと Chainguard の latest が動き、CI が再現しなくなる。
- `user: "0"`: 非 root image が root 所有の使い捨て data dir に書けず起動失敗する（実測）。
- integration job が override を読む: ファイルだけ追加して未使用、を防ぐ。
- 本番 compose 不変: 本番 data dir（/mnt/minio-hdd/minio-data）の image を黙って替えない。

## workflow の shell / permissions / ログ（レビューで P0 として検出）

- `defaults.run.shell: bash`: `pytest ... | tee log` は既定 shell（pipefail なし）だと tee の終了コード
  になり、integration の失敗が緑になる。後続の skip 数チェックは失敗を見ない。
- `permissions: contents: read`: GITHUB_TOKEN を最小権限にする。
- `pytest-integration.log` のアップロード: 失敗時に原因を判断できるログを残す。
