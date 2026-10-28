# ADR-0071: 地図は GeoManifest から描き、位置の検証は決定論的な純関数が行う（LLM の採点で上書きしない）

## Status

Accepted（2026-10-29。契約と純関数だけ。workflow・worker・DB・ArtifactType への接続は後続の枝）

## Context

設計書 v3 §3.2 / §3.3、および Phase 0 監査（docs/architecture/v3-phase0-audit.md）の R6
「史実・地図の誤り」。実際に起きうる事故（テスト T07〜T11・T32 が想定するもの）:

- 地点 A を数百 km 離れた誤地点に描く。画像生成 AI に地図を想像で描かせると、見た目が
  もっともらしいまま位置がずれる。
- 緯度・経度の軸順を取り違える（`(lon, lat)` と `(lat, lon)` の混在）。
- 同名異所（例: 府中）を取り違える。旧称（江戸）を現代の地名として誤認する・現代の国境を
  歴史地図に重ねる。
- 模式図を正確な歴史地図と誤認させる。
- LLM の品質採点（QualityCritic）が高得点を付けて、決定論的に見つかる誤りを素通りさせる。

既存の ADR-0038 の検証は「助言止まり」になりうる（B6 §4）ので、地図は fail-closed の別工程にする。
本 ADR は**その契約と判定規則の部分**だけを決める。どこから呼ぶか（workflow・render 前後の gate）は
決めない。

## Decision

**地図の位置・ラベル・時間基準は `contracts/geo.py` の `GeoManifest` が持ち、`domain/geo` の純関数
`verify_manifest` が参照データ（注入された gazetteer）と照合して `GeoVerdict`（PASS / WARN / REVIEW /
BLOCK）を決定論的に返す。LLM の評価は `combine_verdicts` を通してのみ合流でき、等しいか厳しい側にしか
動かせない。**

- 座標は名前付きの `lat` / `lon`（EPSG:4326、範囲検証）。bbox も名前付き。
- 距離の許容は一律 km ではなく、地物の種類（`GeoFeatureType`）・`location_confidence`・資料の
  不確かさ（`uncertainty_radius_km`、参照側の不確かさ）から決める。許容内 PASS、1.5 倍まで WARN、
  3 倍まで REVIEW、それを超えたら BLOCK。
- 緯度経度の入れ替えは、想定地域 bbox と参照位置の**両方**で検出し、BLOCK。
- 同名の参照候補が複数ある・参照が無い・時間基準が不明・当時の資料の無い現代国境・claim が
  supported / qualified に解決しない、は REVIEW（自動で 1 つに決めない）。
- 模式図に「縮尺不同」の表示が無い・ラベルが未知の地点を指す／地点名と一致しない、は BLOCK。
- 参照データ（`ReferencePlace`）は検証器へ注入する。検証器はネットワーク・ファイル・時計・乱数に触れない。
  テストはメモリ上の固定データ（fake gazetteer）だけを使う。

不変条件 INV-93〜INV-96 と本 ADR を同時に予約する（番号は他の枝と衝突しない: avpu = 0070 / INV-90〜92、
bgm = 0072 / INV-97〜100）。

## Alternatives

- **LLM / VLM に「この地図は正しいか」を判定させる**: 採らない。設計書 §3.1 のとおり LLM の
  「正しい」という言葉は PASS の根拠にならず、T32 の事故（誤りを PASS と評価）を再現する。
- **一律の距離閾値（例: 50 km）**: 採らない。城の位置の誤差と国の重心の誤差は桁が違う。
  一律だと、国には厳しすぎ・城には緩すぎる。
- **schematic の免責の欠落を構築時の ValidationError にする**: 採らない。BLOCK として記録に
  残した方が、gate の結果（何が止めたか）を一貫した形で追える。ただし**契約の外側（LLM の生出力を
  parse する経路）では ValidationError もそのまま fail-closed** で、どちらでも通らない。
- **参照が見つからなければ PASS とする / 候補が 1 つに近ければ自動で採る**: 採らない（fail-open）。
  確証が取れるまで採用しない（設計書 §3.2 検証順 1）。
- **World Historical Gazetteer 等への実接続を本枝に含める**: 採らない。ネットワークは
  AGENTS.md §9 とテスト方針に反し、データ調達・許諾は別の判断（設計書 §3.2 データ候補）。

## Consequences

良い結果:

- 位置・軸順・時代・ラベル・模式図の誤りが、LLM を呼ばず・課金せず・同じ入力から同じ結果で検出できる。
- 結論は check の最悪値と一致する契約（`GeoVerification`）で、BLOCK を PASS として保存できない。

悪い結果・引き受けた負債:

- **未接続**: 本 ADR の時点で、どの workflow もこの検証を呼ばない。INV-93（実在位置を主張する地図は
  manifest を要する）は純関数 `verify_scene` の段階までしか検査できず、「全ての地図シーンがそれを通る」
  ことは接続する枝で検査する（INV-93 に明記）。
- 許容係数（種類ごとの基準 km・信頼度の倍率・1.5 / 3 倍の段）は初期値で、実データで調整が要る。
  値は `domain/geo` の 1 か所（tolerance 定義）に置く。
- 検証できる範囲は gazetteer の質で決まる。参照が無い地点は REVIEW に落ちるので、運用では人の確認が
  増える。`source_ids` が実在の資料を指すかはこの枝では検査しない（claim の評価のみ）。
- 国境の検査は構造化した宣言（`borders` / `border_valid_year`）と文言の語の照合で、地図画像そのものの
  検査ではない。描画後のフレームとの照合（設計書 §3.2 検証順 4・5）は後続。
