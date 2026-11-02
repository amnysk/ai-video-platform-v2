# 新機能 flag の compose 配線と preflight 実効値のテスト根拠（R3）

- `tests/contract/test_compose_feature_flags.py`
  - compose が各 flag を `FEATURE_FLAG_SERVICES` の service「だけ」に `${FLAG:-false}` で渡す: 渡し漏れは
    ON にできない（本番で実際に ENFORCE / GEO / COMPOSED が container に届かなかった）、渡し過ぎは
    service 間で値が割れる。既定 false は OFF 時の挙動を変えないため。
  - entrypoint（run_worker / apps/api）の走査で、flag を読む処理がある service が宣言に入っていること:
    宣言と実コードの食い違い（読み手の追加を宣言し忘れ）を検出する。
- `tests/unit/test_release_effective_flags.py`
  - compose の解決後 environment を注入（オフライン）。`.env` が OFF なのに override で ON、service 間で
    割れる、解決失敗はいずれも UNKNOWN（fail closed）。同じ意味の別表記（1 と true）は衝突にしない。
- `tests/architecture/test_release_preflight_read_only.py`（既存を1語変更）: 許可リストに読み取り専用の
  `docker compose config` を追加した。変更・起動系の動詞の禁止検査は変えていない。
- `tests/unit/test_geo_maps_flag_off.py`（既存を変更）: 旧検査は「未接続なので flag をどのデプロイ
  ファイルにも出さない（接続する枝が変えるまで 0 件）」だった。接続（ADR-0084）済みなので、出てよいのは
  compose.yaml の `${GEO_MAPS_ENABLED:-false}` だけ、に更新した（他ファイルへの露出は引き続き禁止）。
