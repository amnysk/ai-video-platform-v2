# BGM 候補曲（実在・ライセンス確認済みの下調べ）

取得日 2026-11-02。**どれも `license_status=approved` ではない。承認は所有者の行為**（ADR-0083 §2）。
音源は git に入れない。ここは出典・hash・リスクの記録だけ。
ステージ済みの提案カタログ・証拠は取得時の作業領域（`/tmp` 配下、揮発）にあるため、承認する場合は
所有者が証拠を永続領域へ移し、証拠 sha256 を再計算すること。

## 候補

| track_id(案) | 曲 / 作者 | 出典 | ライセンス | 尺 | 積分LUFS | 形式 | audio sha256 | ingest_check |
|---|---|---|---|---|---|---|---|---|
| bgm-pd-shika-no-tone | 鹿の遠音 / 荒木古童III(尺八, 1925-37録音) | Wikimedia Commons `File:Shikanotoone_new.ogg` | PD（録音 PD-Japan-audio、曲 PD-100、ノイズ除去版 PD-self） | 375.1s | -15.1 (LRA 18.2, TP -0.3) | vorbis 44.1k stereo | d63f9714f25b… | accept |
| bgm-cc0-ambient-ck61 | Ambient music test, Yamaha CK61 / Wilfredor | Commons `File:Ambient_music_test,_Yamaha_CK61.flac` | CC0 1.0 | 197.8s | -14.5 (LRA 5.9, TP -1.0) | flac 48k stereo | 713d2fee28e9… | accept |
| bgm-ccby-eastern-thought | Eastern Thought / Kevin MacLeod | incompetech (Commons 経由 mp3) | CC BY 3.0（帰属必須） | 261.9s | -19.9 (LRA 9.8, TP -0.1) | mp3 44.1k stereo | 5daab860de26… | accept |
| bgm-cc0-ambient-kuniholm | Ambient Seattle 2024 / Paul Kuniholm | Commons | CC0 1.0 | 219.3s | -18.0 (TP +0.9) | wav 48k **mono** | a4a67184 4e74… | accept（品質は不採用寄り） |
| bgm-cc0-piano-test-051 | Piano test 051 / Wilfredor | Commons | CC0 1.0 | 581.9s | -22.2 | wav 44.1k stereo | 3c6146b0 9fb5… | accept（練習録音・要試聴） |

ingest_check は sha 一致・object_key が content-addressed・重複/衝突なしだけを見る。**ライセンスの正否は判定しない**。
全曲 `reject_reasons` は `license_not_approved` のみ（= 承認すれば選曲に出る状態）。sha 不一致の入力は `sha_mismatch` で拒否されることも確認。
FreePD.com は 2026-11-02 時点で "Site Closed" のため候補にできない。

## Content ID の注意（既知の範囲。断定しない）

- Commons の CC0/PD 自作録音（Wilfredor 等）: 配信事業者への登録は確認できず、リスクは低いが**ゼロとは言えない**（第三者が同一音を登録している可能性は検索不能）。
- 鹿の遠音: 戦前ビクター盤の復刻。Content ID 登録は確認していないが、復刻盤/コンピレーションが他者に登録されていると誤検出はありうる。
  米国法上の録音の保護期間は国・出版地で判断が分かれる（日本は失効扱い）。**法的な最終判断は所有者**。
- Kevin MacLeod: 本人が Content ID に登録していることが広く知られ、帰属表記があっても自動の申立てが起こりうる（本人宛の許諾連絡で解除する運用）。
  収益化チャンネルでは最もリスクが高い。使うなら CC-BY 帰属文を説明欄に必須で置く。
- 申立て発生時の対処: 証拠（本書の sha/ページ保存）を添えて異議申立て。

## 契約上の不足（報告のみ。コードは変えていない）

1. `LicenseType` に CC0/Public Domain が無い。`custom` + `source_kind=licensed_external` で運ぶしかなく、機械可読に区別できない。
2. `license_checked_at` と証拠 sha を承認時に所有者が入れる前提で、証拠の中身（保存場所）を指す項目が無い（sha のみ）。
3. `exact_attribution_text` は `<` `>` を禁じるのみで改行は可。CC-BY の指定文（複数行）は入る。
4. 配置は `loudness_lufs` 前提で gain=(声基準 -16.5 + 役割相対) - 曲LUFS。-19.9 LUFS の曲は上限 -12 dB が効かず役割差が小さくなる点に注意（ADR-0083 §4）。

## 所有者の承認チェックリスト

- [ ] 各曲の Commons ページ（保存した commons_page.html / commons_api.json）で、ライセンスと原作者を目視確認した
- [ ] 鹿の遠音: PD-Japan-audio / PD-100 の根拠と米国での扱いを許容できる
- [ ] 試聴: ナレーション下で気にならない（ノイズ・無音・不協和・ループ継ぎ目）。60秒を超える曲は先頭から使う
- [ ] 証拠一式を永続領域へ保存し、`license_evidence_sha256` を再計算した
- [ ] `license_checked_at` を入れ、`license_status=approved` に変更（所有者の操作）
- [ ] CC-BY 曲を使う場合: 説明欄に帰属文を全文置ける（fit_credits）
- [ ] Content ID 申立てのリスクを許容する曲だけを承認した
- [ ] 登録は `register_track`（同 sha は重複扱い）
