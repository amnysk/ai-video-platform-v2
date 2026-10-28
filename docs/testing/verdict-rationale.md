# テスト設計の根拠: 共有 Verdict・入力保護・品質レポートの鮮度（ADR-0074、INV-105〜108）

DB・Temporal・provider・ネットワークには触れない純粋なテストだけ。

## INV-105 共有 Verdict

### `tests/contract/test_verdict_contract.py`（契約テスト: import と直列化の互換は契約そのもの）

| テスト | なぜ必要か |
|---|---|
| `test_the_legacy_names_are_the_one_shared_enum` | 別名が将来サブクラスや再定義に戻ると `is` / `isinstance` 比較が割れ、geo の BLOCK を quality が認識しない事故になる |
| `test_wire_values_are_unchanged` | 保存済み JSON（`"block"` 等）が読めなくなると全件が取り込み失敗に倒れる |
| `test_severity_is_defined_once_and_ordered` | 順序が 2 箇所にあった元の不一致（§8）の再発を、同一オブジェクトであることで検出 |
| `test_stricter_is_the_max_and_symmetric` | 全 16 組で `stricter` が最大・可換。LLM が緩められない（INV-94/102）の土台 |
| `test_worst_of_nothing_is_pass_and_block_dominates` | 空の既定値の明示（呼び出し側が無検査を別に扱う前提） |
| `test_existing_models_serialize_identically` | pydantic モデルの dump/validate が別名化で変わらない |

### `tests/architecture/test_verdict_single_source.py`（構造検査: 書き直しは AST で見ないと拾えない）

| テスト | なぜ必要か |
|---|---|
| `test_only_contracts_verdict_defines_the_verdict_set` | 4 値のうち 3 値以上を持つ enum/集合/列/辞書 literal が他にあれば失敗（定義の複製を防ぐ） |
| `test_the_owner_exists_and_is_the_definition` | 検査対象の定義元が消えて検査が空振りしないこと |
| `test_the_detector_catches_a_redefinition` / `test_the_detector_ignores_unrelated_literals` | 検査の検査（見逃しと誤検出） |

## INV-106 信頼できない入力（`tests/unit/test_untrusted_inputs.py`）

実際の漏えいは未発生。Wave 1 のレビュー（d75cfa3 等）で tool id を写していたのと同種の穴が geo の `detail` に
残っていた（`display_name!r` / `label.text!r` / `as_of!r`）ので、敵対的入力から作った単体テスト。
単体にするのは、検証器・契約が純粋で、DB も Temporal も要らず、全組み合わせを安く回せるため。

| テスト | 何を固定し、何を捕まえるか |
|---|---|
| `test_hostile_strings_never_appear_in_a_geo_verification` | 改行・ESC・偽 `sk-` key を地名・ラベル・as_of・主張本文に入れ、検証結果の JSON のどこにも現れない。`detail` に `!r` を戻すと落ちる（変異で確認済み） |
| `test_hostile_strings_never_appear_in_geo_log_fields` | OpenSearch へ載せる `log_fields()` にも漏れない |
| `test_the_hostile_value_is_traceable_only_by_length_and_hash` | 写さない代わりに `value_ref` で辿れる（追跡性を失って「黙る」解にならない） |
| `test_every_check_the_verifier_makes_carries_a_reason_code` | 構造化が一部の検査だけで止まらない。PASS ⇔ OK |
| `test_log_fields_use_only_existing_log_contract_fields` | ADR-0040 の語彙から外れた新フィールドを足さない |
| `test_a_detail_that_could_carry_input_is_refused` | 改行・日本語・タブ・501 字の `detail` を geo / quality の双方が構築時に拒否 |
| `test_a_hostile_report_is_rejected_without_echoing_it` | 約 10k 字の `detail` を持つレポートは取り込めず、例外文にも入力が出ない |
| `test_a_bad_scene_id_is_refused` | `scene_id` に改行・key・不正形式を入れられない |

