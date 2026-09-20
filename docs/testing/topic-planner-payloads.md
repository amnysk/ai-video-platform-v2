# テスト設計の根拠: Topic Planner の Activity 境界（ADR-0029）

## 何を守るか

Temporal は Activity の結果を、workflow 側で**型注釈に従って**復号する。送信側は検査しないので、
型注釈と実際の形が食い違うと Activity は成功するのに workflow task だけが失敗し続ける。
2026-09-20 の実 provider E2E はこれで止まった。fake provider の出力は形が単純で、これを隠していた。

## tests/unit/test_topic_planner_payloads.py

| テスト | なぜ必要か |
|---|---|
| `test_gather_context_with_real_shaped_audience_survives_the_converter` | 事故の再現。実 Analytics と同じ形（内訳の dict が全項目埋まる）を `gather_context` に通し、既定 converter で往復できる。内訳が失われず読めること、`totals` が窓だけであることも固定 |
| `test_gather_context_with_partial_audience_survives_the_converter`（3ケース） | 取れなかった項目は None。scalar だけ・内訳だけ・空でも往復できる（None / 空の扱いで形が変わらない） |
| `test_a_context_the_workflow_could_not_decode_degrades_to_no_analytics` | 復号失敗は Activity の例外ではなく workflow task の無限失敗。fallback の梯子（Activity の例外だけを捕まえる）に届かない失敗クラスを、返す直前の往復検査で `no_analytics` に劣化させる。ログに値・例外文が出ないことも固定 |
| `test_ensure_decodable_keeps_a_good_context_unchanged` | 正常な context を勝手に劣化させない |
| `test_every_topic_planner_boundary_type_round_trips`（9型） | 契約の dataclass すべてを非空の実形状で往復。他の型に同種の食い違いが無いことの固定点 |
| `test_boundary_table_covers_every_dataclass_in_the_contract` | 契約に型を足したのに往復の表へ足し忘れる、を防ぐ |
| `test_snapshot_payload_round_trips_the_full_audience` | 保存した snapshot を読み戻しても内訳が同じ（保存形を変えていない） |
| `test_snapshot_saved_before_the_breakdown_fields_still_loads` | 内訳の項目が増える前に保存された snapshot が読める（本番 DB に既存） |
| `test_an_unreadable_saved_snapshot_degrades_to_no_analytics` | 読めない保存済み payload で Activity が例外を出し続けて Planner が止まらない |

## tests/unit/test_topic_planner_workflow.py（追加1件）

`test_real_shaped_audience_breakdown_reaches_the_prompt_through_the_worker`: worker（time-skipping Temporal、
本番と同じ converter）経由で、内訳つきの Analytics が最後まで通り prompt に載る。修正前のコードでは
`RuntimeError: Failed decoding arguments` で 30 秒のタイムアウトになることを確認済み（本番のエラーと同じ）。
復号失敗は待っても終わらないので `asyncio.wait_for` で失敗にする。
