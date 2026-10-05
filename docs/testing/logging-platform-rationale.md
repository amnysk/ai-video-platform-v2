# ログ基盤（収集・検索側）のテストが存在する理由

対象: ADR-0040 の `deploy/logging/`・`scripts/gen_log_mapping.py`・アプリ compose の `logging:`。
実測の根拠は ADR-0040 と [platform.md](../observability/platform.md)（OpenSearch 3.8.0 /
Fluent Bit 5.1.2、2026-09-30）。

## 1. `tests/contract/test_log_contract_mapping.py`

- **生成物が最新であること**: mapping と Collector の型表は `contracts/log_contract.py` から生成する。
  フィールドを足して再生成を忘れると、新しいフィールドは `dynamic: false` で黙って検索できなくなり、
  Collector は未知のキーとして `attributes.collector_moved` へ退避する。どちらもエラーにならないので、
  検査しなければ気付けない（AGENTS.md §7 の「片側だけの更新」）。
- **写像規則（型ごと）**: boolean・keyword・text に `ignore_malformed` を付けると index template の登録が
  400 で拒否される（実測）。逆に数値・日付に付け忘れると、型の合わない1件で bulk item が 400 になり
  Fluent Bit が上限まで再送して破棄する。`@timestamp` に付けると値の無い文書が Dashboards の時間軸から
  消えるので、付けないことも固定する。
- **infra の mapping は app の部分集合で、型が同じ**: 両系統を1つの index pattern 群で検索したときに
  同名フィールドの型が食い違うと Dashboards が conflict として扱う。
- **Lua の型表と label 定数**: Collector の振り分け（`avp.logging=app`）と型修復は Lua の表を読む。
  表と契約がずれると、アプリの行が infra 系統へ流れる・正しい値が退避される。
- **`--check` の終了コード**: CI や手元で drift を検出する手段として使えることを固定する。

## 3. `tests/integration/test_logging_collector_lua.py`

本物の `fluent-bit.yaml` の filter 列を採用版イメージ（5.1.2）で通す。Lua は手元にインタプリタが無く、
Fluent Bit の Lua 実装（LuaJIT）と msgpack 変換の癖（配列と map の区別、null の扱い）は実物でしか
確かめられないため、unit test にしない。

- **compose project の完全一致**: containers/ の mount は全コンテナのログを見せる。他 project の行
  （ログ基盤自身・別の試験環境）が混ざらないことを固定する。
- **`avp.logging=app` でも stderr・JSON でない行・壊れた JSON は infra（unstructured）**: アプリの
  起動失敗の traceback や alembic の出力を app の mapping に入れると型不整合で bulk item が 400 になる。
- **型修復**: boolean に `"yes"`、keyword に object、未知のキー、アプリが書いた Collector のフィールドを
  `attributes.collector_moved` へ退避し `collector_errors` に名前を残す。これをしないと 1件の 400 が
  retry 上限まで再送され、同じ chunk の再送回数を消費する（ADR-0040 §5）。
- **`@timestamp` を record から外し record の時刻へ移す**: 出力側（`time_key`）が `@timestamp` を1つだけ
  書く。重複キーは ingest pipeline 経由で bulk 全体を 400 にする（実測）ので、構造的に起きない形にする。
  不正な値は Docker の時刻へ置換し `@timestamp_replaced`。
- **`_id` にできない `event_id` の退避**: 512 bytes を超える `_id` は item ではなく bulk の **request 全体**が
  400 になり、同じ chunk の正常な行も 72 回の再送（実測 約2時間50分）の末に破棄された（隔離環境で実測、
  正常2件を含む3件が `dropped_records_total`）。Collector で退避して自動 ID にする。
- **ミリ秒の丸め**: 出力側は record 時刻の `tv_nsec` を切り捨ててミリ秒を書く。`.001` を double にすると
  `.000999…` になり 1ms 早い時刻が保存された（隔離環境で実測）。stdout も同じ切り捨ての iso8601 で検査する。
- **追加の安全化と切り詰め**: 整形器の取りこぼし（`Authorization: Bearer …`、DSN の userinfo）が
  OpenSearch へ届かないこと、unstructured 行が `UNSTRUCTURED_LINE_MAX_BYTES` 以下になることを固定する。
