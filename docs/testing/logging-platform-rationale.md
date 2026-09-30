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
