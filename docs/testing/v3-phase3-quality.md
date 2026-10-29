# テスト設計の根拠: 品質検査の記録・必須の集合・手動承認（ADR-0081、INV-126〜132）

各テストについて「どのリスクか / どの不変条件か / なぜこの層か / 何が落ちたら何を捕まえたことになるか」を残す。
実データ由来の事故ではなく、Phase 2 報告 §3.1/§4 の「ON にすると全件 REVIEW」と、検査結果の使い回しという
設計上の穴から作った。

## `tests/unit/test_quality_records.py`（純粋。DB・時計・ネットワークなし）

層の理由: 判定は純関数（`domain/quality/records.py`）なので、DB と切り離して全組み合わせを安く固定できる。
DB の構造（CHECK・一意・トリガ）は別の層（migration 契約・integration）で固定する。

| テスト | リスク / 不変条件 | 捕まえるもの |
|---|---|---|
| `test_a_set_must_cover_exactly_the_gates_of_its_key` | INV-127: BGM セットに `MUSIC` だけ入れて `LICENSE` を未検査のまま PASS にする | gate の過不足があるセットが書けてしまう |
| `test_an_llm_check_cannot_be_recorded` | INV-102: 記録に LLM の検査を混ぜて決定論の根拠に見せる | `deterministic=False` の記録 |
| `test_expiry_is_fixed_to_the_maximum_validity` | INV-107/126: 期限を長く自称して古い結果を使い続ける | 期限が `MAX_REPORT_VALIDITY` 以外 |
| `test_naive_times_and_bad_hashes_cannot_be_recorded` | INV-126: タイムゾーン無しの時刻・形の違う hash で比較が壊れ、一致扱いになる | aware でない時刻 / 64 桁 hex でない hash |
| `test_usage_declaration_requires_evidence_iff_used` | INV-127: 根拠の無い「使用」・根拠付きの「不使用」という矛盾した宣言 | 宣言の整合 |
| `test_a_record_for_another_final_video_hash_is_not_counted` | INV-126 中核: 前回の動画で通った結果を再 render 後に使う | 別 sha の記録が fresh に入る |
| `test_a_record_for_another_episode_is_not_counted` | INV-126: Episode を取り違えた記録 | 別 Episode の記録 |
| `test_a_record_for_another_input_hash_is_not_counted` | INV-126: 同じ sha でも入力が違う記録 | 別 input_hash の記録 |
| `test_a_record_for_an_older_rule_version_is_not_counted` | INV-107/126: 規則更新後に旧規則の PASS が残る | 別 rule_version の記録 |
| `test_an_expired_record_is_not_counted_and_the_boundary_is_exclusive` | INV-107: 期限ちょうど / 直前の境界（`now >= expires_at` は失効） | off-by-one |
| `test_a_record_from_the_future_is_not_counted` | INV-101: 時計異常・改ざんで未来日付の記録を受け入れる | 未来日付 |
| `test_a_record_with_an_incoherent_validity_is_not_counted` | INV-107: DB 外から入った期限が長すぎる行 | 有効期間が上限超 |
| `test_the_newest_generation_wins_…` | 追記専用 + 世代: 再検査で BLOCK になった結果を古い PASS が隠す | 古い世代の採用 |
| `test_a_declaration_for_another_hash_is_not_used` | INV-127: 別 hash への「不使用」宣言で BGM 検査を免除する | 別 hash の宣言の再利用 |
| `test_everything_present_and_fresh_with_declared_non_use_is_allowed` | 正常系（必須が全部揃い、不使用が宣言済み）が許可される。拒否しかしないゲートでは運用できない | 過剰な拒否 |
| `test_no_records_at_all_is_refused_as_review` | INV-101/127: 記録ゼロは PASS ではない | 無検査の PASS |
| `test_each_always_required_check_is_required` | INV-127: 技術 QA / 説明欄のどちらを欠いても通らない（それぞれ 1 ケース） | 片方の欠落の見落とし |
| `test_an_undeclared_feature_is_refused_not_treated_as_unused` | INV-127 中核: 「宣言が無い = 使っていない」と読む fail-open | 宣言欠落の PASS |
| `test_a_used_feature_requires_its_check_set` | INV-127: 使ったのに検査が無い / 検査が有れば通る | 使用宣言と検査の不整合 |
| `test_a_used_feature_with_a_stale_set_is_refused` | INV-126 × 127: 使用なのに検査が別 hash のもの | 古い検査での通過 |
| `test_a_recorded_block_is_refused_even_when_everything_else_passes` | INV-102: 他が全部 PASS でも決定論の BLOCK は通らない | BLOCK の希釈 |
| `test_required_keys_follow_the_declarations` | 必須の集合の導出 | 導出の誤り |
| `test_the_provider_does_not_raise_on_empty_input` | gate は例外を PASS に倒さない / 空でも REVIEW を出す | 例外 |
| `test_a_fake_producer_and_writer_satisfy_the_protocols_and_flow_to_an_allow` | ADR-0081 §7: BGM / 地図の枝が差し込む口の形（Protocol）が実際に繋がる | 口の形の食い違い（pyright と実行の両方） |
| `test_a_producer_that_returns_nothing_leaves_the_set_missing` | producer の失敗は記録なし = 必須欠落 = 拒否 | 失敗の PASS 化 |
