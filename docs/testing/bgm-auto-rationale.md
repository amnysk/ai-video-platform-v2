# ADR-0087 のテストの設計理由（BGM 自動収集・自動選曲・フォールバック・品質ゲートの検査障害）

テストを足すたびに、「なぜそれがテストになったか」（守る事故・不変条件・その段階を選んだ理由・捕まえる失敗）を
同じコミットでこの文書に足す。

## 契約（`tests/contract/test_music_acquisition_contracts.py`）

- `test_only_wikimedia_commons_is_auto_acquirable`: 「無料でダウンロードできる」を「自動で使ってよい」と取り違える事故
  （依頼 §3.1）を、方針表という 1 箇所の定義で止める。ソースを足したときに表を書き忘れると全列挙の一致で落ちる。
  契約の段階なのは、収集・選曲・検査の全てがこの表を読むから（AGENTS.md §8）。
- `test_a_track_without_the_new_fields_still_parses_with_safe_defaults`: 既存の `catalog.json`・保存済みの証跡が
  契約の拡張で読めなくなると、BGM が黙って外れる（`BgmSourceError`）。既定値が「承認者なし・リスク不明・持ち上げ 0」と
  安全側であることも固定する。
- `test_a_policy_approval_must_name_its_policy_version`: 規則による承認が版を持たないと、規則を直しても古い自動承認が
  残り続ける（INV-151）。契約で作れなくする。
- `test_gain_offset_has_a_bounded_range`: 持ち上げ幅の入力誤り（+60 等）で BGM が声を潰す事故を形で止める
  （実際の上限は描画側の `BGM_NARRATION_PRIORITY_MAX_DB`。ここは多層防御）。
- `test_the_fallback_ladder_is_bounded`: 梯子の上限が増えると描画前の測定が増え、無限ループに近づく（INV-155）。
  値を変えるなら ADR を先に直させる。
- `test_a_selection_decision_without_a_track_is_valid_no_bgm`: 「BGM 無し」は正当な決定で、保存できなければ再実行の
  たびに選曲がやり直される（INV-153）。
