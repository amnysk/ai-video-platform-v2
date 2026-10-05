# ログ基盤の版（ADR-0040 §7）

compose.logging.yaml は下の **index digest**（multi-arch）で固定する。`latest` や tag だけの指定は使わない。
linux/amd64 の manifest digest は、取得したイメージの検証用（`docker buildx imagetools inspect <ref>`）。
`tests/contract/test_logging_platform_config.py` がこの表と compose の一致を検査する。

| コンポーネント | tag | index digest | linux/amd64 manifest | 確認日 |
|---|---|---|---|---|
| OpenSearch | 3.8.0 | `sha256:fafe3fc3587088674669235575aa166228c48bdb940294a8cdbbc1da75236a40` | `sha256:68a688de28fb9bb66601552650b91a52a9fd5e7eac5481dd2b225ecb66fd09b0` | 2026-09-30 |
| OpenSearch Dashboards | 3.8.0 | `sha256:7fb7ec1b33f1ef49796dc31180f17f227361263c15bb19c56a8a6e5a38852bbd` | `sha256:647672283fbb49907bb67743b1995ba6da663327d5181760d7e622ecccada1ce` | 2026-09-30 |
| Fluent Bit | 5.1.2（commit 66910c10） | `sha256:d792375ca8e53be72fc25716c28f291f32c6fc6f4f31d12d0d14bc78cefe9226` | `sha256:71cda445290efc2d45d565c12e0de4b15aa0182510276ad50c317aae1423d7ee` | 2026-09-30 |

## 互換の根拠（実測・ソース、2026-09-30）

- OpenSearch と Dashboards は同じ版（3.8.0）でなければならない（Dashboards は版の違う cluster を拒否する）。
- OpenSearch 3.x は bulk の action 行の `_type` を 400 で拒否する（実測）→ Fluent Bit は
  `suppress_type_name: on` 必須。
- Fluent Bit 5.1.2 の opensearch output は bulk 応答の item ごとに 2xx と 409 を成功扱いにし、失敗 item
  だけを再送する（`src/flb_search_bulk.c`）。ただし action 行そのものが不正（`_id` が 512 bytes 超など）だと
  **request 全体が 400**（item 単位ではない）になり chunk 全体が再送・破棄される（実測）。
- Fluent Bit の `tls.verify_hostname` は既定 off（`-o opensearch -h`）→ 明示的に on。
- `create` + `id_key`: 既存 `_id` は 409 で成功扱い（重複抑制）。
- OpenSearch 3.8.0 は `ignore_malformed` を boolean に付けると template を 400 で拒否する。
- ingest pipeline（`index.default_pipeline`）を使うと、bulk 内に JSON として壊れた文書が1件あると同じ bulk の
  全文書が 400（実測）。Fluent Bit は msgpack から JSON を作り直して送るので重複キー・壊れた JSON は生じない。

## 見送った版

- OpenSearch / Dashboards 3.9.0（2026-09-29 公開。確認時点で公開1日）。index digest
  `sha256:adfa61f85025d06b4aeb562e7e74fde7e31c437039c93c3862c17e9acebd6c7c` /
  `sha256:4bdb8ded547ce7f4c379a10261f1b4aad58726900c215be570a7d1fe86f2a7d8`。上げるときは
  platform.md §7 の確認を同じ commit で再実行する。
