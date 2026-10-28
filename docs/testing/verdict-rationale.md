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

## INV-107 品質レポートの鮮度

実事故ではなく、設計 v3 T22 の拡張（規則を更新した後に古い PASS が投稿を通す、再 render 後の PASS が残る）。
純粋関数で時計を注入できるため単体テスト。DB・Temporal・実時計は使わない。

### `tests/unit/test_quality_freshness.py`

| テスト | 何を固定し、何を捕まえるか |
|---|---|
| `test_a_fresh_report_is_allowed_…` | 鮮度の検査が決定論だけのレポート（model なし）を過剰に拒否しない |
| `test_a_report_for_another_artifact_hash_is_refused_even_if_pass` | 別 artifact（再 render）の PASS を流用できない。`check_freshness` と `evaluate_for_upload` の両方で |
| `test_a_report_for_an_older_rule_version_is_refused` / `test_an_empty_current_rule_version_matches_nothing` | 規則を更新したら古い PASS は無効。空の「今の版」が何にも一致する fail-open を防ぐ |
| `test_expiry_is_judged_against_the_injected_clock` | 失効の境界（直前は許可・ちょうど/後は拒否）。`now` だけで決まる |
| `test_the_decision_does_not_read_the_real_clock` | 過去日付でも結果が注入した `now` で決まる（実時計に依存しない） |
| `test_an_incoherent_expiry_is_refused_not_trusted` | `expires_at` が検査以前・上限超過・naive の自称を信じない |
| `test_the_maximum_validity_is_allowed_exactly` | 上限の境界（ちょうど許可）。上限が 1 箇所であることの裏づけ |
| `test_an_ai_check_without_a_model_identity_is_refused` / `test_a_model_identity_cannot_carry_free_text` | AI を使った検査はどの AI か辿れる。同一性に自由文（INV-106）を入れられない |
| `test_the_report_round_trips_with_its_freshness_fields` | 生成側と取り込み側が同じ形（片側だけの変更で全件拒否に倒れるのを防ぐ） |
| `test_a_report_without_a_freshness_field_cannot_be_parsed` | 鮮度の項目が欠けたレポートが既定値で通らない |
| `test_a_schema_version_1_report_is_refused_as_a_schema_mismatch` | 鮮度を持たない旧形は拒否 |
| `test_a_naive_clock_is_refused_not_raised` | 総関数性（INV-101）を鮮度でも維持 |
| `test_staleness_beats_a_pass_verdict_but_not_a_block` | 期限切れ・BLOCK のどちらも通さない（鮮度が BLOCK を緩めない） |

### `tests/architecture/test_quality_clock_injected.py`

`contracts/quality.py` と `domain/quality` が `datetime.now()` 等の実時計を読まない（読むと単体テストで失効を再現できず、
本番だけで時刻依存の欠陥になる）。検査器が違反 fixture を捕まえることも別テストで示す。

### 既存テストへの変更（理由）
`tests/unit/test_quality_upload_decision.py` と `tests/contract/test_quality_contract.py` は、契約の形の変更
（`rule_version` / `expires_at` / `model` / `schema_version` 2、`current_rule_version`）に合わせて**入力の組み立てだけ**を
更新した。assert は一切変えていない。

