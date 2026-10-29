# ADR-0084: 歴史地図の結線（依頼 → 検証 → 決定論の描画 → Production の事前検査 → Render の差し替え・証跡）

## Status

Proposed（オーナーの承認待ち。番号 0084 と INV-139〜145 は Phase 3-D の予約）。
**既定は OFF**（`GEO_MAPS_ENABLED=false`）で、OFF の間の挙動は現行とバイト単位で同じ（INV-143）。
本番へ入れる判断は別途（§9 の有効化条件）。

## Context

ADR-0071 / 0078 は地図の契約・検証・描画・見た目の検査の部品を入れたが、どの workflow にも接続していない
（Phase 2 報告 §4-6: 結線・hash の結び付け・CJK フォント）。守るもの:

- 誤った位置・時代錯誤・根拠不足の地図を**投稿しない**（設計 v3 §3.2、INV-93〜96）
- 地図に有料の画像生成（fal 等）を使わない（INV-119）
- 現行の本番の挙動・Temporal の履歴を壊さない（INV-4 / INV-120）

## Decision

### 1. 依頼の作り手と決定論性（INV-139）

**manifest の座標は gazetteer からだけ**取る。地図の指定（`GeoMapSpec`）は次の**どちらか一方**:

- `GeoMapRequest`: 座標を**持たない**依頼。地点は gazetteer の `ref_id` で指し、名前・claim・source だけを書く。
  `domain/geo/manifest_builder.py`（純関数）が gazetteer から座標・地物の種類・存在期間・不確かさを埋めて
  `GeoManifest` を作る。依頼の作者（人・Codex）が座標を書き間違える経路が無い。`extra=forbid` なので座標を書くと
  契約違反。gazetteer に無い `ref_id` / 重複 / 根拠（claim・source）が空の地点は manifest を作らず BLOCK
  （推測で埋めない）。
- 完成済みの `GeoManifest`: LLM などが座標ごと書いた manifest。位置違い・軸の取り違えを `verify_manifest` が止める
  （既存。負例の検証で使う）。

依頼の**出どころ**: 新しい有料モデルは足さない。供給元は port（`GeoSource`）で、本 ADR の実装は非 DB の
`LocalDirectoryGeoSource`（`${ai_video_work_root}/geo/` の JSON。BGM の供給元と同じ形）。依頼を storyboard / 台本から
**自動で起こす**部分（既存の Codex 経路での提案）は**未実装**で、後続の枝が同じ `GeoMapRequest` 契約へ書き込む。
自動で起こす場合も採否は §3 の決定論の関門が決め、LLM の採点では上書きしない（INV-94）。

### 2. 供給元のファイル（オーナーが供給するデータの契約）

```
{root}/gazetteer.json            GazetteerFile: license と curated_by が必須（空は読まない）
{root}/basemap.geojson           任意。下地の陸地（Polygon / MultiPolygon）。無ければ点とラベルだけ
{root}/{episode_id}/specs.json   GeoMapSpecs（この Episode の地図の指定）
{root}/{episode_id}/evidence.json  research の EvidenceArtifact（claim・source・links）
```

`specs.json` が無い Episode は「地図なし」。**あるのに読めない・契約違反**は `GeoSourceInvalidError`（needs_input）で
止める（読めないものを「地図なし」にすると検査が黙って外れる）。ネットワークには出ない（INV-144）。
research の evidence は本来 MinIO の research artifact から取る。MinIO 版の供給元は後続（port の差し替え）。

### 3. 検証（INV-140）

`domain/geo/map_gate.gate_map` が、既存の `check_map_accuracy`（ADR-0078）に **gazetteer・claims・sources を必ず渡して**
検証する（`sources` を渡さないと出典の解決が見られない）。見るもの（すべて既存の検査）: 座標と gazetteer の距離
（地物ごとの許容）/ 軸の取り違え / 想定地域の外 / 地点の存在期間と `as_of` / 名前が `as_of` に使われていたか
（旧称・現称）/ 国境の基準年 / claim の解決・評価・種類 / source が取得済みの資料に解決するか / ラベルと地点名の一致。
**PASS 以外（BLOCK も REVIEW も）の地図は描かれず使われない**。曖昧（同名・参照なし・時間基準不明）は REVIEW で、
自動で 1 つに決めない。描いた地図は見た目の検査（ADR-0078）も PASS でなければ使われない（画像は証跡に残る）。

### 4. 決定論の描画・クリップ・CJK

- 描画は既存の `render_map`（Pillow・乱数/時計/ネットワークなし）。下地（任意の `BaseMap`）を足した。下地なしの出力と
  identity は従来と同じ。下地を使うと**必ず注記**（`Modern coastline (Natural Earth)`）が地図に描かれ、見た目の検査の
  対象になる（現代の海岸線を当時の地形に見せない）。国境線は描かない。
- 解像度は render profile（Shorts は 1080x1920）。クリップは既存の `build_map_clip_argv` を render worker が起動時に
  sha256 検証した固定版 ffmpeg で実行（`infrastructure/geo/clip.py`。bitexact。実行の場所はここだけ）。
  尺は**シーン動画 Artifact の実尺**（計画の trim / freeze がそのまま効く）。
- CJK: 描画は render worker が sha256 検証済みの `render_font_path`（Noto Sans CJK）のバイト列を使う。フォントに
  無い字は `glyphs_missing` → `FONT_MISSING_GLYPHS` で BLOCK（INV-145）。フォントの sha256 は render identity に入る。

### 5. hash の結びつき（INV-141）

`GeoMapRecord`（`contracts/geo_scene.py`）が鎖を 1 つに束ねる:

```
manifest ─manifest_sha256→ 検証 ─verification_sha256→ 画像 ─png_sha256→ クリップ ─clip_sha256→ 完成動画 ─final_video_sha256
        gazetteer_sha256 / evidence_sha256（判定の入力）・basemap_sha256・render_identity_sha256・font_id も記録
```

`domain/geo/map_binding.binding_problems` が manifest / verification は**再計算**して照合し（自己申告を信じない）、
PASS の記録がクリップ・完成動画を欠く／別の完成動画のものなら問題として返す。

Render は完成動画 1 本ごとに `GeoRenderEvidence` を MinIO に保存する（DDL なし・新しい ArtifactType なし）:
キー `artifacts/{episode}/geo_map_evidence/{binding_id}.json`、`binding_id = sha256({final_video_sha256, input_hash})`
（`domain.geo.map_binding.evidence_binding_id`。**ゲートも同じ式で引ける**: final_video の sha256 と行の input_hash から）。
**final_video の行を記録する前**に保存する（証跡の無い動画を作らない）。`GEO_MAPS_ENABLED` が ON の Render は
地図のない Episode でも**必ず**書く（`records` 空 = 「地図を使っていない」の明示）。DB が要る場合の計画は末尾。

### 6. 品質ゲートへの引き渡し（INV-142）

`domain/geo/quality_checks.map_quality_checks(*, final_video_sha256, records, intended_scene_ids=())` →
`tuple[QualityCheck, ...]`（gate = GEO、`deterministic=True`、`evidence_ref` = 記録の sha256）。純関数。

| 状況 | verdict | reason_code |
|---|---|---|
| 地図なし（記録も指定も無い） | PASS | OK（`evidence_ref` = `sha256({"geo":"not_used","final_video_sha256":…})`） |
| 検証・見た目・鎖が PASS | PASS | OK |
| 位置違い・根拠不足・時代錯誤など（正確さが PASS でない） | BLOCK / REVIEW | GEO_MISMATCH |
| 時間基準が不明だけ | REVIEW | GEO_TIME_BASIS_UNKNOWN |
| 見た目だけの欠陥（豆腐・重なり等） | BLOCK / REVIEW | VISUAL_DEFECT |
| 指定したのに証跡が無い・2 つ以上・hash の鎖が切れている・別の完成動画の証跡 | BLOCK | GEO_MISMATCH |

`detail` は固定文と理由コードだけで、入力の文字列（地名・主張）を入れない（INV-106）。**既存の `QualityReason` で足りる**
（`contracts/quality.py` は変更しない）。永続化・投稿可否は `claude/v3-p3-quality` 側（この関数を呼ぶ）。
`intended_scene_ids` は `GeoRenderEvidence.intended_scene_ids` から取れる。

### 7. Production / Render への結線と Temporal の replay 安全性（INV-143）

**workflow のコード・Activity の追加/順序変更・入出力 dataclass は一切変えない**（BGM の ADR-0077 §7 と同じ方針）。
地図の処理はすべて**既存の Activity の内側**で、設定は worker の組み立て（`run_worker.py`。唯一の読み手）で読む。
したがって `workflow.patched` / 版管理は不要で、既存の履歴は従来どおり再生できる
（`tests/architecture/test_geo_boundaries.py::test_workflows_and_activity_contracts_do_not_import_the_wiring`）。

- **Production（`ProductionActivities.plan`）**: `GeoPreflight`（任意の引数。None なら何もしない）が、有料呼び出しの
  **前**に同じ関門 `gate_map` で指定を検査し、通らなければ `GeoMapRefusedError`（needs_input）で止める。
  fal を呼ぶ前に人間へ返す。地図の指定が無い Episode は何もしない。
- **Render（`RenderActivities._render`）**: `GeoRenderService`（任意の引数）が ①指定を読み、地図ごとに関門→描画→見た目
  の検査 ②`render_input_hash(geo=...)` に判定・画像・クリップ長の指紋を入れる ③作業領域で PASS の地図だけクリップに
  して**そのシーンの入力動画を差し替える**（他のシーンは従来のまま）④完成動画の sha256 が決まったあと証跡を保存。
  拒否された地図は差し替えず（従来の動画のまま）、理由コードをログに残す。**投稿の拒否は品質ゲート**（§6）が行う。
- `render_input_hash` の `geo` は ON のときだけ入る（OFF は**キーごと無い**ので既存 hash はバイト単位で同じ）。ON では
  地図が無くても印が入る → 証跡の無い従来の動画を再利用せず、一度描き直して証跡を書く。
- **fal の節約は未実装**: Render が差し替えるため、地図のシーンの fal 動画は Production で従来どおり生成される
  （課金は変わらない）。地図のシーンで fal を呼ばない経路は、画像/動画 Activity の「再利用」の返り値の形
  （`reservation_id=""` + artifact）で workflow を変えずに実現できるが、production worker に ffmpeg が要る・予約台帳との
  整合があり、本 ADR の範囲外（§10 の未実装）。

### 8. 失敗の扱い

| 事象 | 扱い |
|---|---|
| 指定ファイルがあるのに読めない・契約違反・gazetteer のライセンス記載なし | `GeoSourceInvalidError`（needs_input）。地図なしにしない |
| 指定が storyboard のシーンと台本シーンの対応に合わない | 同上 |
| 地図が PASS でない（Production plan） | `GeoMapRefusedError`（needs_input）。fal の前に止める |
| 地図が PASS でない（Render） | 差し替えない。証跡に残し品質ゲートが投稿を拒否 |
| クリップの ffmpeg が失敗 | `RenderEngineFailedError`（retryable。既存の retry） |

### 9. 設定キーと有効化条件（AGENTS.md §8）

設定キーは既存の `Settings.geo_maps_enabled`（`GEO_MAPS_ENABLED`、既定 `false`）**1 つだけ**。他は足さない。
実装後の `grep -rniE "geo_maps_enabled" apps/ workers/ domain/ infrastructure/ contracts/`（9 件）の分類:

| 場所 | 分類 | 件数 |
|---|---|---|
| `workers/production/run_worker.py` / `workers/render/run_worker.py`（属性の読み） | 読み手（worker の組み立て。この 2 箇所だけ） | 2（+ エラー文 1） |
| `infrastructure/config.py`（`Settings.geo_maps_enabled`） | 定義 | 1 |
| `infrastructure/media/map_scene_visual.py` / `infrastructure/geo/guard.py` / `contracts/geo_scene.py` / `workers/render/activities.py` / `domain/render/identity.py` | 無関係（docstring・コメント・エラー文の言及） | 5 |

読み手が 2 箇所だけであることは `tests/architecture/test_geo_boundaries.py::test_the_setting_is_read_only_where_workers_are_assembled`
（属性アクセスの AST 検査）が守る。
`compose.yaml` / `.env.example` には**出さない**（既存の `test_the_flag_is_not_exposed_in_any_deployment_file`
が 0 件を要求。有効化の判断時にオーナーが compose へ通し、その検査を ADR で更新する）。

有効化の前提（起動時に `require_quality_gate` が検査し、満たさなければ worker が起動しない）:
`AVP_QUALITY_GATE_ENFORCE=true`（拒否された地図の投稿を止めるのが品質ゲートなので）。加えて運用の前提:
①オーナーが `gazetteer.json`（ライセンスと出典つき）を供給 ②地図を使う Episode の `specs.json` / `evidence.json`
③`basemap.geojson`（任意。Natural Earth はパブリックドメイン）④production / render worker から `{root}/geo` が読める
mount ⑤`claude/v3-p3-quality` のゲートが `map_quality_checks` を呼んでいる。

### 10. 未実装・既知の限界（黙って捨てない）

1. 依頼を storyboard / 台本から**自動で起こす**経路（Codex 提案）。現状は人（または別の agent）が `specs.json` を書く
2. research evidence を MinIO から読む供給元（現状は JSON ファイル）。gazetteer / basemap の DB 化
3. 地図のシーンで fal を呼ばない経路（§7）
4. 下地は**現代の海岸線**。当時の地形・国境の地図データは無い（国境は描かない。`borders=historical` でも線は描かない）
5. 見た目の品質は暫定（点とラベルと陸地のみ。縮尺・方位・時代の装飾なし）
6. 品質ゲートでの呼び出し・永続化（別の枝）

## DDL が必要になる場合の計画（本 ADR では書かない）

gazetteer / specs / 証跡を DB に置くなら、`geo_gazetteer_places`（ref_id, 名前の履歴, 座標, license, curated_by, version）・
`geo_map_specs`（episode_id, spec JSON, sha256）・`geo_map_evidence`（episode_id, final_video_sha256, input_hash,
object_key, sha256）を新設する。他表へ FK を張らない設計で、ロールバックは 3 表の `DROP`。別の承認済み枝で採番する
（migration 0019 は別 agent の所有）。

## Alternatives

- **LLM が書いた manifest をそのまま使う（座標つき）**: 位置の正しさを検証器だけに頼る。座標を依頼に書かせない案の方が
  失敗の経路が少ない（残す経路は負例の検証用）
- **地図の BLOCK で Render を失敗させる**: 日次が止まり fal 費用が無駄になる。Production の事前検査で有料呼び出しの前に
  止め、Render は証跡と品質ゲートに任せる
- **Production の workflow に地図の分岐を足す**: Activity / 履歴の互換を壊す。既存 Activity の内側で完結させる
- **画像生成 AI で地図を描く**: 位置の保証が無い（INV-119 が禁止）
- **証跡を ArtifactType にして manifest に入れる**: production の input_hash に絡む。BGM の証跡と同じく MinIO のみ

## Consequences

良い結果:
- OFF で本番の入力 hash・ffmpeg コマンド・Temporal の履歴が変わらない
- 座標を書き間違える経路が無く、誤った地図は fal の前（Production）と投稿の前（ゲート）の二重で止まる
- 完成動画に使われた地図は hash の鎖で検証・画像・クリップ・完成動画に結びつく

悪い結果・引き受けた負債:
- **gazetteer の実データはオーナー供給**（本リポジトリには無い）。実データの精度が検証の精度の上限
- 地図のシーンの fal 費用は節約できない（§7）
- 有効化には compose の通し・既存の flag 検査の更新・ゲートの配線が要る（意図した手間）
