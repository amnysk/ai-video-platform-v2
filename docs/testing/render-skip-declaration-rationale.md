# render skip 経路の使用宣言（ADR-0086 追補 R4 / INV-149）: テストを置いた理由

## tests/unit/test_render_skip_declaration.py

| テスト | 防ぐ失敗 |
|---|---|
| `test_a_pre_enforce_render_gets_both_not_used_declarations_on_the_skip_path` | ENFORCE を ON にする前に描画された動画が宣言を持たず永久に REVIEW（自動投稿が止まる）。補完で動画を描き直す・行を増やす・対象 hash を取り違える |
| `test_the_skip_path_declares_once_and_is_idempotent` | skip のたびに宣言が増える / 同じ主張が unique 制約で例外になる |
| `test_the_skip_path_writes_nothing_while_enforce_is_off` | flag OFF の skip 経路が DB を書く（INV-130 / 導入前と同一でなくなる） |
| `test_a_bgm_identity_in_the_hash_is_never_declared_not_used` | BGM を混ぜた動画を「使っていない」と宣言し、gate が BGM を検査せず通す（ライセンス・発話余裕・クレジットの素通り） |
| `test_a_geo_identity_in_the_hash_is_never_declared_not_used` | 地図入りの動画を「使っていない」と宣言し、BLOCK すべき地図が通る |
| `test_a_failing_declaration_on_the_skip_path_does_not_fail_the_render` | 補完の DB 障害で再利用できる描画が失敗扱いになる。例外の本文がログに出る |

Temporal replay: 変更は Activity の内部のみ（workflow・Activity 名・dataclass 署名は不変）なので、CI の `temporal-replay`
（`tests/unit/test_*_replay.py`）が従来どおり通ることを確認する。
