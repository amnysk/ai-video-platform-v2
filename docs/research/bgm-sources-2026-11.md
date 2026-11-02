# BGM 音源ソースの規約調査（2026-11）

調査日: **2026-11-02**。ADR-0087 §1 と `contracts/music_sources.py` の表の根拠。規約が変わったら表・この文書・
`tests/contract`（`test_bgm_source_policy_doc.py`）を同じコミットで更新する。**法的助言ではない**。
下の「確認できなかった点」は断定せず、自動取得を許す根拠にしない（許可は表で `auto_acquire_allowed=True` のソースだけ）。

## 結論（表との整合）

ADR-0087 §1 の表は**調査結果と矛盾しない**。変更なし。補足:

- Free Music Archive: 公開 API の現状を一次資料で確認できなかった（2018 年に閉鎖が告知され、その後 KitSplit が取得。
  Terms of Use は自動リクエストとデータマイニングを禁じる）。表のとおり自動取得は ×。
- Pixabay: 公式 API 文書（images / videos のみ）に音楽の endpoint は無い。規約 §8 は自動収集を禁じる。表のとおり ×。
- Commons の API 応答の「ライセンス」はアップロード者の申告で、WMF は正確性を保証しない（Commons:Reusing content）。
  そのため自動承認は CC0 + 本人の作品 + 制限・係争なしに限り、残りは REVIEW（INV-150）。**CC0 詐称は機械では見抜けない**
  （ADR-0087 Consequences に記載済み）。

## ソース別

| 項目 | Wikimedia Commons | Free Music Archive | Pixabay Music | ccMixter | incompetech | Openverse | Freesound | Musopen |
|---|---|---|---|---|---|---|---|---|
| YouTube の商用利用 | ライセンス次第。CC0 / CC BY / PD は可 | 曲ごとの CC。NC は不可 | Pixabay License で可（単体の再販は不可） | 曲ごと。NC は不可 | CC BY なら可（収益化も可。帰属が必要） | 曲ごと（集約。正確性は無保証） | CC0 / CC BY は可、NC は不可 | 曲ごと。NC / ND あり。PD 表示も録音の権利は国で異なる |
| 自動検索・取得 | **可**（MediaWiki API。UA・serial・maxlag を守る） | 規約で自動リクエスト・データマイニング禁止 | 規約 §8 で自動収集は強く禁止。API に音楽無し | Query API あり（beta）。ライセンスは曲ごと | API 無し。規約で自動利用に言及無し | API あり（認証・利用規約は別途） | API あり（取得は OAuth 要。大量取得は Data Packs） | API 無し |
| 再配布・改変 | CC0 は可。BY は帰属、BY-SA は継承 | 曲ごと | 改変可。単体の再販不可 | 曲ごと（NC / SA / ND 混在） | 改変可（改変部分の明示） | 曲ごと | CC0 可 | 曲ごと |
| 帰属義務 | CC0: 無し。BY: 作者・ライセンス・リンク | 曲ごと | 不要（推奨） | 曲ごと | 必要（タイトル・作者・ライセンス） | 曲ごと | CC0: 無し | 曲ごと |
| Content ID リスク | 不明（CC0 でも他者が登録する例はある） | 不明 | 申し立て例が多い（規約は Content ID に言及無し） | 不明 | 不明（ISRC で出所を示せる） | 不明 | 低〜不明 | 申し立て例あり（フォーラム） |
| 取得方法 | API の `imageinfo.url` の原本 URL | 手動 | 手動 | 手動（Query API の URL は将来検討） | 手動 | 提供元に従う | 手動（ログイン要） | 手動 |
| 証拠の保存 | API 応答の正準 JSON の sha256 + 原本 URL + 取得時刻 | 手動で保存 | ダウンロード時の画面・規約の版 | 曲ページの保存 | 規約のコピーと曲ページ（サイトが推奨） | 元ソースの証拠を別途 | API 応答 | 曲ページ |
| 停止時の代替 | 既存の承認済み曲で制作継続 + 所有者が手で供給 | — | — | — | — | — | — | — |
| 自動取得 | **○（CC0 のみ自動承認）** | × | × | × | × | ×（初期は対象外） | ×（初期は対象外） | × |

## 出典（すべて 2026-11-02 に取得）

### Wikimedia Commons / MediaWiki

- User-Agent 方針: https://foundation.wikimedia.org/wiki/Policy:Wikimedia_Foundation_User-Agent_Policy
  - 形式 `<client>/<version> (<contact>) <library>/<version>`。連絡先（メール・URL）を入れる。"bot" を含めるのが推奨。
    ブラウザの UA の流用・`python-requests` 等の既定値は禁止（403 になりうる）。
- API エチケット: https://www.mediawiki.org/wiki/API:Etiquette
  - 要求は**直列**（並列にしない）。非対話の処理は `maxlag` を付ける。UA を必ず付ける（無いと予告なしの IP ブロック）。
    読み取りに固定の上限は無いが過負荷を避ける。`ratelimited` は指数バックオフ。
- 再利用: https://commons.wikimedia.org/wiki/Commons:Reusing_content_outside_Wikimedia
  - 帰属は**作者**（アップロード者とは限らない）。PD でも出所の表示を推奨。WMF は権利情報の正確性を保証しない。
    アップロード者が作者でない例・人格権・商標など著作権以外の制限がありうる。
- 実測（2026-11-02、読み取りのみ）: `generator=search` + `prop=imageinfo|categories|templates` +
  `iiprop=url|size|mime|sha1|extmetadata|mediatype` が音声ファイルの `extmetadata`（License / LicenseShortName / Credit /
  Artist / Restrictions / Categories 等）と `sha1`、原本 URL を返すことを確認。実装はこの形で固定（fixtures: `tests/fixtures/bgm_commons/`）。

### Pixabay

- 規約: https://pixabay.com/service/terms/ — 商用利用可（単体の再販不可）。§8 は「データマイニング・スクレイピング・ロボットによる
  自動収集」を禁じ、大量・系統的な複製は明示の許可が要る。音楽固有の規則・Content ID の言及は無い。帰属は不要。
- API: https://pixabay.com/api/docs/ — **images と videos のみ**（音楽の endpoint 無し）。100 req / 60 s、24 時間キャッシュ必須、
  「系統的な大量ダウンロードは不可」。

### Free Music Archive

- 検索結果（https://en.wikipedia.org/wiki/Free_Music_Archive , https://freemusicarchive.org/Terms_of_Use ）: 2018 年に閉鎖告知、
  その後 KitSplit が取得。Terms of Use は自動リクエスト・データマイニングを禁じ、曲ごとのライセンスに従う。
  https://freemusicarchive.org/api は 404（API の現状は確認できず）。

### ccMixter

- https://ccmixter.org/query-api — Query API 2.0（beta）。ライセンスは by / nc / sa / nod / byncsa / byncnd / sampling 系 / pd。
  サイトの規約として商用可否の一括の記述は無く、曲ごとのライセンスに従う。NC と BY が多く、帰属の自動化が未検証。

### incompetech

- https://incompetech.com/music/royalty-free/faq.html — CC BY。YouTube（収益化含む）で可。帰属（タイトル・"Kevin MacLeod
  (incompetech.com)"・ライセンス）必須。改変可（自分の部分を明示）。著作権フリーでも PD でもない。Content ID は別記事に誘導。
  ISRC が出所の証明に使える。API・自動利用への言及無し。Commons の複製（例: Eastern Thought）は CC BY 3.0 のため REVIEW。

### Openverse / Freesound / Musopen

- Openverse: https://docs.openverse.org/api/reference/made_with_ov.html — 音声を含む。**ライセンス情報の正確性は保証しない**と明記。
  集約なので出所のソースの証拠が別途要る。
- Freesound: https://freesound.org/help/faq/ — CC0 / CC BY / CC BY-NC。ダウンロードはログイン要。大量取得は Data Packs。
- Musopen: FAQ（https://musopen.org/faq/ ）と第三者の記事 — PD 表示でも国によって録音・作曲の権利が異なる。曲ごとに CC BY-NC 等がある。
  規約の原文（https://musopen.org/terms-of-service/ は 404）は確認できず。

## 確認できなかった点（断定しない）

- FMA の API の現状、ccMixter の利用規約の商用の記述、Openverse の API 規約の詳細、Musopen の規約の原文。
  どれも自動取得の対象にしないので方針には影響しない。解禁するなら一次資料を確認してこの文書と表を更新する。
- Content ID の実際の発生率は調査していない。`content_id_risk` は自動取得で `unknown`（選曲の点数を下げる）。

## 証拠の保存と停止時の対応（Commons）

- 保存: API 応答（imageinfo + categories + templates）の**正準 JSON**と取得 URL・時刻を MinIO `music/evidence/<sha256>.json`、
  原本を `music/originals/<sha256>.<ext>`。sha1（Commons 報告）とダウンロードしたバイトの一致も検査する。
- 停止（API 変更・ブロック）: 収集だけが止まる。既存の承認済み曲で制作は続く。所有者が `scripts/bgm_catalog_import.py` で手で供給する。
