# BGM 自動収集（ADR-0087 §1〜§3、INV-150〜152）のテストの設計理由

テストごとに「守る事故」「捕まえる失敗」「その段階を選んだ理由」を書く。足したら同じコミットで更新する。

## 方針表と規約調査（`tests/contract/test_bgm_source_policy_doc.py`）

- `test_every_searchable_source_is_covered_by_the_research_doc` / `test_only_commons_is_auto_acquirable_and_the_doc_says_so`:
  表（`contracts/music_sources.py`）だけ直して規約調査の文書が古いままになる事故（前身 repo の「片側だけ更新」）を止める。
  契約の段階なのは、実装が無くても表と文書の食い違いは起きるから。

## ライセンスの事実の取り出し（`tests/unit/test_bgm_commons_facts.py`）

- 実在の Commons 応答 5 件（`tests/fixtures/bgm_commons/`）で CC0 本人 3 曲・CC BY・PD を取り出せること: 判定の入力が
  実データの形と食い違うと、規則が正しくても全件が REVIEW/REJECT になる（または逆に通る）。合成データだけでは気付けない。
- `test_evidence_sha256_is_...`: 「判定は保存した証拠に結び付く」（INV-151）の土台。証拠の sha が正準 JSON の hash でないと、
  後から同じ証拠で再判定できない。
- `test_strip_html_is_deterministic...` / `test_dangerous_markup_never_collapses_to_a_clean_own_work`: Credit に埋め込んだ
  マークアップ（コメント・script）で「Own work」が偽装されるのを止める。除去結果が決定的でないと証拠 hash と判定が揺れる。
- `test_deletion_and_dispute_signals_...`: 削除依頼・著作権侵害のカテゴリ/テンプレート（隠しカテゴリを含む）を見落とす
  事故。API の categories/templates と extmetadata の両方を見る。
- `test_formatversion_2_list_pages_are_accepted`: API の応答形式が変わっても（dict/list）読めること。ユニットで十分
  （純関数）。

## 自動承認の規則（`tests/unit/test_bgm_license_policy.py`、INV-150）

- `test_adversarial_table`: 「曖昧なものは自動承認しない」を表で固定する。`CC0?`・`Public domain / CC0`・NC/ND（短い名前・URL・
  利用条件のどこに出ても）・ライセンス識別子の食い違い・Restrictions・削除カテゴリ・空/他人の Credit。実際の実装は最初の版で
  `CC0?` を句読点の除去で CC0 と読んで自動承認しており、この表が捕まえた（正規表現で記号の混じる表記を unknown にして修正）。
- `test_only_one_input_combination_class_approves`: 判定に効いていない事実（死んだ条件）が無いこと。1 つ壊すと必ず落ちる。
- `test_non_auto_acquirable_source_never_auto_approves`: 「無料ダウンロード可 ≠ 自動で使ってよい」。CC0 の事実が揃っていても
  incompetech 等は REVIEW。
- `test_registered_track_is_always_quarantined_and_unverified` / `test_approve_track_requires_an_auto_approve_decision`:
  登録は常に隔離、承認済みの曲は規則を再適用した結果としてしか作れない（呼び出し側が判定を偽って渡せない）。

## 検索語（`tests/unit/test_bgm_search_terms.py`）

- 決定性と上限（4 件）、固定語彙以外が検索構文へ流れないこと（`insource:` 等の注入）。収集の回数上限と再現性の土台。

## 音源の検査（`tests/unit/test_bgm_audio_inspect.py`、実 ffmpeg・無ければ skip）

- 壊れたファイル・短すぎ/長すぎ・無音・映像つきを弾く、合成した曲でラウドネスが取れる、ffprobe 無しの経路でも読める。
  モックでは ffmpeg の出力形式（ebur128 の Summary）の変化を検知できないので実バイナリで確かめる。第三者の音源は使わない。

## Commons クライアント（`tests/unit/test_bgm_commons_client.py`、`httpx.MockTransport`）

- User-Agent の検証・直列・maxlag の再試行（1 回）・429/5xx/タイムアウトは一時障害・redirect を追わない・ホストと拡張子の許可表
  （要求を送る前に拒否）・バイト上限（宣言と実測の両方）・sha1 照合。Wikimedia の方針違反（ブロックの原因）と、
  悪意ある/壊れた応答での過大ダウンロードを止める。ネットワークは出ない（ソケット接続を禁じる fixture つき）。

## 収集サービス（`tests/unit/test_bgm_acquisition_service.py`、INV-152）

- 隔離登録→承認、REVIEW は昇格しない、REJECT は何も書かない、解析失敗は登録しない、重複は書かない、レート制限、
  回数/容量/時間の上限、読み戻しの不一致、候補の失敗で run が続くこと、プログラム誤りは握りつぶさないこと。
  偽の source とインメモリ catalog（`tests/support/bgm_catalog_fake.py`）で DB・通信無しに全分岐を通せる。
  実 ffmpeg を使う 1 本（`test_end_to_end_with_the_real_inspector...`）で結線を確かめる。
- DB の実装（B の枝）との一致は fake が port の契約（同 sha256 は書かない・隔離でなければ ValueError）を写すことで担保する。
  実 DB での確認は統合テストの段階。

## Workflow / worker（`tests/unit/test_bgm_acquisition_worker.py`）

- 起動条件（両 flag が true でなければ SystemExit・連絡先が無ければ SystemExit）: OFF の間はネットワークに触れない（INV-159）。
- 時間スキップの Temporal で、逐次・上限件数・1 件の失敗が他を止めない・試行 2 回・空は何もしない。Workflow を決定的に保つ。

## 境界（`tests/architecture/test_bgm_acquisition_boundaries.py`）

- 音楽でネットワークを import してよいのは `infrastructure/music/sources/` だけ、render / production / upload は収集を
  import しない、worker が接続前に flag を検査する、時計を注入する。実行時でなく構造で止めるのは、一度 import されると
  どの経路からでも通信が起きうるから。
