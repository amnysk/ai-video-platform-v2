# fal 残高不足への耐性（ADR-0057 / INV-71〜74）のテスト設計根拠

事故の形: fal の残高が尽きるとアカウントがロックされ、401/402/403 で submit が拒否される。
旧実装はこれを (1) 認証 incident（10分窓で抑止が解ける）、(2) 入力の拒否（入金後も恒久ブロック）、
(3) 課金された前提の確定（費用の過大計上）として誤記録し、本文を捨てるので原因も後から分からない。
本番には再送してはいけない未照合予約（`591d1a8b…`）も残っている。
fake / `httpx.MockTransport` だけを使い、fal・YouTube には一切到達しない。

## 分類と本文要約（`tests/unit/test_fal_refusal.py`、unit）

| テスト | 守るもの / 落ちたら何が起きているか |
|---|---|
| `test_status_alone_never_decides_balance` | 402/403 でも本文の印が無ければ残高不足と確定しない。公式文書に残高の構造化コードは無く、status だけで決めると鍵の権限エラーを「入金待ち」と誤認する |
| `test_balance_marker_beats_status_for_402_and_403_and_401` | 印があれば status に依らず残高不足。403 で出た本番の観測を 402/401 でも取りこぼさない |
| `test_401_without_balance_marker_is_auth_invalid` | 印の無い 401 は従来どおり認証（ADR-0030 の経路）。残高に寄せすぎない |
| `test_three_statuses_map_to_three_different_kinds` | 401/402/403 がそれぞれ別の種類になる（要件: 異なる理由 → 異なる kind） |
| `test_markers_live_in_one_place_with_provenance` | 判定語は1表だけ・出所つき（AGENTS.md §8）。出所の無い印を足せない |
| `test_structured_and_list_bodies_are_searched` | `detail` が文字列・配列・入れ子の `error.message` のどれでも印を見つける |
| `test_summary_redacts_secrets_urls_and_truncates` | 偽の API キー・Authorization・署名付き URL（`?token=` / `X-Amz-Signature`）が要約に残らない。長さの上限 |
| `test_summary_of_unreadable_body_is_explicit` | 本文が読めないことを「空」ではなく明示する（原因不明を捏造しない） |
| `test_submit_refusals_use_distinct_error_types` | queue の submit が3種の例外に分かれる。残高/分類不能は `ProviderUnavailableError` の子ではない（認証 incident に数えない） |
| `test_401_diagnostic_body_is_kept_in_the_message` | G0: 以前は捨てていた 401 の本文が診断として残る |

既存テストの変更（理由: ADR-0057 で設計が承認された仕様変更）:
`tests/unit/test_fal_queue.py` の「本文なし 403」と `tests/unit/test_fal_storage.py` の 403 は、
`ProviderUnavailableError` ではなく `ProviderRefusalUnknownError` を期待するようにした
（本文に印が無い 403 を認証と決め打たない = ADR-0030 の「原因を断定しない」の趣旨を、安全側の
`UNKNOWN`（breaker を開く）へ進めた）。401 は変わらない。
