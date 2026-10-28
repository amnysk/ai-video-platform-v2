# ADR-0078: 歴史地図の正確さの関門と決定論的な地図描画（既定 OFF・未接続）

## Status

Accepted（2026-10-29。純関数・描画部品・設定 flag まで。workflow・Activity・QualityGate への接続は含めない）

## Context

ADR-0071 は GeoManifest と検証器（Wave 1）を決めたが、(a) 検証の結果は check 名と自由文 `detail`
の列で、QualityGate が機械的に読める理由コードを持たない、(b) 歴史地図で誤った史実を提示する種類の
指摘（アナクロな地名・現代国境・根拠の欠落）は REVIEW で、「upload 前に BLOCK」にならない、
(c) manifest から画像を描く部品が無く、地図は画像生成 AI の想像に頼るしかない（設計書 v3 §3.2）。
さらに「読みにくいが正確」な地図と「読みやすいが不正確」な地図を一つの点数にすると、見た目が
正確さを救う（T32 と同型の事故）。

## Decision

**flag `GEO_MAPS_ENABLED`（`Settings.geo_maps_enabled`、既定 False）の背後で、次の 3 つを純粋・
決定論的な部品として追加する。どれも workflow / Activity から呼ばれない（接続は後続の枝）。**

1. **歴史的正確さ**（`domain/geo/map_accuracy.py`、`contracts/geo_map.py`）。Wave 1 の
   `verify_manifest` / `verify_scene` の結果を `MapAccuracyResult`（verdict + `MapFinding` の列）に写す。
   finding は `reason_code`（`MapReasonCode`、唯一の定義）・`target_kind` / `target_id`・
   `evidence_claim_id` / `evidence_source_ids` だけを持ち**自由文を持たない**。検証器との差は
   REVIEW → BLOCK の一方向の引き上げだけ（緩める経路は無い）:
   - 歴史地図（`historical`）: アナクロな地点・名前・ラベル、現代国境、国境の年代違い、
     根拠のない国境の主張 → BLOCK。
   - 模式図以外: claim が解決しない / 支持されない・資料が取得済みに解決しない・claim の評価が
     食い違う → BLOCK（「根拠に結びつかない位置を提示しない」）。
   - 同名異所（`AMBIGUOUS_PLACE`）・参照なし（`UNKNOWN_PLACE`）・時間基準不明・不確かさの過大申告は
     REVIEW のまま。**自動で 1 つに決めない**（ADR-0071）。曖昧さを解くのは manifest の
     `place_id`（参照 id）だけ（既存どおり。表示名が参照の既知の名前でなければ止まる）。
   - 模式図は `schematic_disclaimer` が無ければ BLOCK（INV-96。既存）。描画時にも画像へ表示する。
2. **見た目の品質**（`domain/geo/map_presentation.py`）。`MapLayout`（描画器が返す幾何）に対し、
   ラベルの重なり・最小フォント px・コントラスト比・ラベルのキャンバス内収まり・模式図の免責の
   可視性を検査し、`MapPresentationResult`（別の理由コード `PresentationReasonCode`）を返す。
   **正確さとは別の関数・別の結果**。`combine_map_results` は `stricter` でしか合流せず、見た目が
   良くても正確さの BLOCK / REVIEW は変わらない。見た目の最悪は REVIEW（読めない地図は人が見る）。
   例外は模式図の免責が画面外・不可視の場合で、INV-96 の表示要件なので BLOCK。
3. **決定論的描画**（`infrastructure/maps/`）。同じ GeoManifest・同じ `MapRenderSpec`・同じ
   フォントのバイト列から**同じ PNG のバイト列**を作る。Pillow（既存依存、`constraints.txt` で
   固定）の `ImageDraw` のみ。投影・フォント・サイズ・色・描画順（place_id 昇順）を固定し、乱数・時刻・
   PNG の text chunk を持たない。出力は ffmpeg の静止画入力で、`infrastructure/maps/clip.py` が
   既存の `ffmpeg_graph` の「収め方」「seconds」を再利用した argv を組み、既存 render 線の
   シーン映像として使える mp4 にする。**fal・画像生成 provider を import しない**（architecture test、
   INV-119）。描画しない国境: `borders != none` でも国境線は描かない（データを持たない。
   正確さの判定側で止まる）。

### 接続しない理由（Activity を足さない）

実接続には (i) storyboard / production のシーン視覚の入力としてのアーティファクト種別、
(ii) 描画 Activity、(iii) QualityGate への配線（別担当）のいずれかが要り、Activity の追加・並べ替えや
dataclass の変更は workflow replay を壊す（INV-1x、既存 replay テスト）。本 ADR はそこで止める:
純関数と描画ビルダーを flag OFF で置き、production identity・storyboard・render の出力が
**1 バイトも変わらない**ことを golden で固定する（INV-120）。

### §8 grep（flag 名）

`grep -rniE "geo_maps|GEO_MAPS_ENABLED" apps workers domain infrastructure contracts docs tests compose*.yaml`
→ 本 ADR 前: 0 件。読み手 0 / 書き手 0 / 無関係 0。追加後の読み手は `infrastructure/maps/scene_visual.py`
の 1 箇所（flag を引数で受ける純関数）、書き手は `infrastructure/config.py` の 1 箇所、compose へは
**追加しない**（未接続の flag を本番 env に露出しない）。
環境変数名は既存の慣習（接頭辞なし。例 `PLANNER_TREND_ENABLED`）に合わせ `GEO_MAPS_ENABLED`
（依頼文の `AVP_GEO_MAPS_ENABLED` ではない）。

## Alternatives

- **検証器（Wave 1）の REVIEW を BLOCK に変える**: 採らない。ADR-0071 の契約と既存テストを変える
  ことになり、地図以外の用途（現代地図）の判定も変わる。写像側で一方向に引き上げる。
- **正確さと見た目を 1 つの点数にする**: 採らない。見た目が正確さを救う。別の結果・別の理由コード、
  合流は `stricter` のみ。
- **LLM / VLM に地図の正しさを判定させる**: 採らない（ADR-0071 と同じ）。
- **Remotion・ブラウザ描画**: 採らない（ADR-0019）。
- **新しい描画ライブラリ（cairo・matplotlib 等）**: 採らない。Pillow（既存）で足りる。フォントの
  グリフ依存の差異は、フォントのバイト列の sha256 を描画の同一性に含めて検出する。
- **画像生成 AI に地図を描かせる**: 採らない。位置が想像になる（本 ADR の動機そのもの）。

## Consequences

良い結果:

- 誤った歴史地図（座標・時代・国境・根拠）が、LLM も課金もなしで、理由コード付きで upload 前に BLOCK
  される材料になる。
- 同じ manifest から同じ画像。差分は manifest の差分だけ。
- 見た目の良さが正確さの判定を変えない。

悪い結果・引き受けた負債:

- **未接続**: 「全ての地図シーンがこの関門を通る」「画像が実際に描画される」は接続する枝で検査する。
  QualityGate が `check_map_accuracy` / `check_map_presentation` を呼ぶ配線は別担当。
- 描画の同一性はフォントのバイト列・Pillow の版に依存する。版を上げたら golden の再確認が要る
  （`constraints.txt` で固定）。同梱フォント（Pillow 既定）は ASCII のみで、日本語ラベルは
  `RENDER_FONT_PATH` と同じ CJK フォントを渡す必要がある。グリフ欠落は検出しない（未対応・後続）。
- 描画しない国境・陸海の地形は無い（点とラベルだけ）。これは「模式図」と同等の情報量であり、
  `historical` の地図として提示するには背景地図データ（許諾・調達は別の判断）が要る。
- 時代・国境の BLOCK 引き上げの対象は初期判断。実データで REVIEW に戻す余地があるが、戻すなら本 ADR を
  更新する（テストを緩めない）。
- 見た目の閾値（最小フォント 28 px、コントラスト 4.5:1 など）は初期値で、実データで調整する。
- 予約済み番号: INV-117〜120（本 ADR）。
