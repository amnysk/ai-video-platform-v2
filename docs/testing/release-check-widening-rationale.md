# CHECK 語彙の拡張の免除: 各テストが存在する理由

対象: `domain/release_preflight.py` の `CHECK_WIDENING` 規則、migration 0017（ADR-0060, INV-82）。
免除規則は「走査を緩める変更」なので、**通るものより通ってはいけないもの**のテストが主役。

## tests/unit/test_release_check_widening.py

| テスト | 防ぐ誤判定 |
|---|---|
| `test_a_declared_strict_superset_widening_passes` | 規則が空振りして #10 が BLOCKED のまま（本来の目的） |
| `test_the_widening_is_reported_not_silently_ignored` | 免除が黙って行われ、レビュアーが preflight 出力から気づけない |
| `test_the_real_0017_is_recognised_as_a_widening` | 規則のテストが合成サンプルだけで、実ファイルの書き方が規則に乗っていない |
| `test_without_a_declaration_the_same_drop_is_still_blocked` | 規則が「CHECK の drop + create なら何でも通す」に緩んでいる |
| `test_a_declaration_that_is_not_a_strict_superset_is_blocked` | 同一集合・縮小・旧値を落とす差し替え・重複・空の旧集合が「拡張」として通る（旧コードが書く値の喪失） |
| `test_the_created_values_must_equal_the_declared_new_set` | 宣言は正しいのに実 SQL には宣言外の値を足す／値を減らす |
| `test_only_a_plain_column_in_list_condition_is_accepted` | `OR 1=1`・`NOT IN`・`IS NOT NULL`・別列・`; DROP`・引用符すり抜けの文字列を create に混ぜる |
| `test_a_non_literal_condition_is_not_accepted` | 変数の条件式は静的に読めないのに通る |
| `test_anything_that_is_not_exactly_the_declared_widening_is_blocked` | 別名の制約の drop、unique / foreignkey / type 省略の drop、別名での作り直し、別の表、create 無し（制約を消すだけ）、create が先、`with` の外、同じ with への他操作、2 回目の drop が通る |
| `test_other_destructive_calls_in_the_same_file_are_still_blocked` | 免除が同じファイルの `drop_column` / `DELETE FROM` まで隠す |
| `test_a_widening_hidden_in_a_helper_is_judged_the_same_way` | helper 経由でも判定が同じ（走査は upgrade から呼ばれる関数を辿る既存の性質を保つ） |
| `test_only_a_module_level_literal_declaration_counts` | 文字列・コメント・関数内・変数を使う宣言・キー過不足でのなりすまし |
| `test_downgrade_is_not_scanned_and_cannot_smuggle_an_upgrade_drop` | downgrade 側の drop まで免除に巻き込む／upgrade から呼ばれない関数を走査する |
| `test_the_marker_route_is_unchanged_for_other_contract_migrations` | 規則追加で通常の contract migration の扱いが変わる |

## tests/contract/test_migration_0017_check_widening.py（SQLite）

0012 は CHECK を `for k in AnomalyKind` で作るため、新規 DB は 0012 の時点で新語彙を持つ（`_as_production_at_0015` が
本番の旧語彙の形を再現する）。この前提が無いと「0016 で新値が拒否される」ことを確かめられず、0017 のテストが
何も踏まない。

| テスト | 防ぐ誤判定 |
|---|---|
| `test_declared_old_set_is_what_0016_actually_accepts` | 宣言の `old` が実際の旧制約と食い違い、「旧値は通る」という主張が根拠を失う（走査は宣言しか見ない） |
| `test_new_set_is_every_AnomalyKind_the_code_can_write` | コードの enum に値を足したのに CHECK を足し忘れる／宣言が enum とずれる |
| `test_after_upgrade_old_kinds_still_work_new_kinds_work_unknown_still_rejected` | 拡張で旧コードの書き込みが壊れる（expand-only の核心）／制約が全開になる |
| `test_rows_written_by_old_code_survive_the_upgrade` | 制約の作り直しで既存行が消える・検証で落ちる |
| `test_downgrade_refuses_while_new_kind_rows_exist_and_changes_nothing` | downgrade が新 kind の監査行を黙って消す（独立レビュー E の P2-8。旧テストは「新 kind の行を消す」を期待していたが、仕様を ADR-0060 の追記で「拒否」に変えた）／拒否したのに行・版・制約が変わる |
| `test_downgrade_keeps_old_rows_and_restores_the_old_constraint_when_no_new_rows` | 拒否が過剰で行が無くても戻れない／旧行まで消す／旧制約に戻らない／往復できない |
| `test_the_release_scanner_accepts_this_migration_as_a_widening_and_nothing_else` | 実ファイルを書き換えて規則から外れ、preflight が再び BLOCKED になるのを CI で検知する |

## tests/integration/test_migration_against_postgres.py

SQLite は `batch_alter_table` で表を作り直すが、PostgreSQL は `ALTER TABLE ... DROP/ADD CONSTRAINT`。
`test_0017_widens_the_kind_check_and_keeps_old_code_working_on_postgres` は実 PostgreSQL で同じ主張
（旧値は通り続ける・新値が通る・未知は拒否・downgrade で戻る）を確かめる。

## 実データでの確認（テストではなく手順の記録）

- 本番スキーマ（`pg_dump --schema-only`、alembic 0015）を使い捨て PostgreSQL に復元して upgrade head / downgrade 0016 / upgrade head
- 本番コード（80775c0）の `OperationalAnomalyRepository` を、0017 適用後のスキーマに対して実行:
  旧 enum の 9 種全てを書き込め、新 kind の行が混ざっていても `list_open` / `pending_notifications` が動く
- `migration_compat` を 本番(80775c0) 対 #10 head で実走: PASS（detail に `CHECK widening ... 9 -> 11`）。
  変更前は BLOCKED（`drop_constraint`、ADR が deploy 済みでない）

## 追記: 宣言を信用しない（独立レビュー E の P2-3）

`CHECK_WIDENING` は作者の宣言で、そのままでは PK/FK/UNIQUE の名前を CHECK と偽って drop を免除でき、
`batch_alter_table(recreate="always", copy_from=...)` の表の作り直しも見逃していた。
- `test_a_declaration_naming_a_non_check_constraint_is_blocked`: `*_pkey` / `*_key` / `uq_*` / `fk_*` / `pk_*` / `ck_` で
  始まらない名前の宣言は BLOCKED（0017 の `ck_operational_anomalies_kind` は通る＝過剰拒否でない）。
- `test_batch_alter_table_keywords_void_the_exemption`: recreate / copy_from / table_args / reflect_args / schema の
  どれか 1 つでも付いたら免除しない。
- `test_a_raw_drop_next_to_a_valid_widening_is_still_blocked`: 生 SQL の `DROP CONSTRAINT`・`op.drop_constraint`
  が有効な拡張の隣にあっても、そちらは従来どおり BLOCKED（免除は 1 組だけ）。
- `test_a_narrowing_is_blocked_...`: old が new の上位集合（縮小）は宣言不正。
- `tests/unit/test_review_e_defect_demos.py` の 2 件は xfail を外して通常の回帰テストにした。
- 既存の type_ 欠落・別 type・余分な操作・順序違いは既に `test_anything_that_is_not_exactly_...` が持つ。
