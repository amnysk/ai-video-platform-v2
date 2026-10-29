# ADR-0086: BGM・地図の品質検査を記録つきゲートへ結線する（使用宣言・証跡の結び付け・発話余裕の実測・クレジット）

## Status

Proposed（オーナーの承認待ち。INV-146〜148 は本 ADR の予約）。
**新しい設定キーは無い**。`AVP_QUALITY_GATE_ENFORCE`（ADR-0081）・`BGM_ENABLED`（ADR-0077）・
`GEO_MAPS_ENABLED`（ADR-0084）の既存 3 つの背後にだけ入り、すべて OFF なら DB にも MinIO にも書かず、
挙動は結線前とバイト単位で同じ。migration なし。workflow・Activity の追加/変更なし。

## Context

ADR-0081 は「BGM・地図が使われたとき検査は必須」の仕組み（`CheckSetProducer` の差し込み口・`declare_usage`）を作ったが、
差し込む側が無かった。ADR-0083 / 0084 は純関数（`bgm_quality_checks` / `map_quality_checks`）と、完成動画に結び付く
証跡の保存までを作った。つなぐと次の 4 つの穴が実際にあった（実コードで確認）。

1. **BGM の証跡の key が内容の hash**（`artifacts/{ep}/bgm_render_evidence/{証跡の sha}.json`）。gate は prefix 列挙の
   Port を持たないので、完成動画から証跡を引けない（地図は `geo_evidence_key` で引ける）。
2. render が使用宣言を書かない。宣言が無いと gate は全件 REVIEW（`USAGE_DECLARATION_MISSING`）。
3. 発話中の声と BGM の差は Activity で測られない（ADR-0083 の負債）。全 BGM 動画が REVIEW になる。
4. 説明欄へ BGM のクレジットを渡す呼び出し元が無い。upload 工程と gate の説明欄 lint は
   `build_upload_metadata` を別々に呼ぶので、片方だけ `credits=` を渡すと lint した文と投稿した文が食い違う。

もう 1 つ、**「対象の動画 sha」に 2 種類ある**ことに注意する。gate の `CheckSubject.final_video_sha256` は
final_video JSON の正準 hash（`artifact_metadata.sha256`）。BGM・地図の証跡と純関数が使う `final_video_sha256` は
**媒体（mp4）の sha256**（`FinalVideoArtifact.media.sha256`）。producer は後者で証跡を引き、前者で記録を書く。

## Decision

BGM・地図の枝は、次の 4 点だけを足す。判定の純関数・ゲートの判定規則・記録の表は変えない。

### 1. 証跡は完成動画と入力 hash から引ける key に置く（INV-147）

BGM の証跡の key を `artifacts/{ep}/bgm_render_evidence/{sha256({final_video_sha256: 媒体 sha, input_hash})}.json` にする
（地図の `evidence_binding_id` と同じ式。唯一の組み立て口は `infrastructure/music/evidence.py::bgm_evidence_key`。
render と gate と説明欄が同じ関数を使う）。object type 名は `contracts/music_render.py::BGM_EVIDENCE_OBJECT_TYPE` が唯一の定義。
`BgmRenderEvidence` に省略可能な `speech`（§3）を足す（旧 JSON は `speech=None` として読める）。
`BGM_ENABLED` が ON のときだけ起きる変更で、OFF の出力は変わらない。旧 key（内容 hash）の証跡は読まれなくなり、
該当する動画は「宣言が無い」ので REVIEW になる（まだ ON にされた実績が無いので移行はしない）。

### 2. render は final_video の行を記録した後に使用宣言を書く（INV-146）

`AVP_QUALITY_GATE_ENFORCE` が ON のときだけ、`RenderActivities` が `QualityRecordWriter` を受け取り
（worker の組み立て口で `build_quality_record_writer` が決める。flag を読むのは worker の組み立てだけ）、
行を記録して commit した後に `declare_usage` を BGM と MAP の 2 つ書く。

- BGM: `BgmResolution.applied` のとき **used**、`evidence_ref` = 証跡の正準 sha256。それ以外は **not used**。
- MAP: `GeoPlan.prepared` が 1 件以上のとき **used**、`evidence_ref` = 証跡の sha256。**拒否された地図（BLOCK / REVIEW）も used**
  （使わないと宣言すると gate が検査せず BLOCK に届かない）。地図の指定が無い・`GEO_MAPS_ENABLED` が OFF は **not used**。
- 失敗は非致命: 例外は型名だけログに出し、描画は成功のまま。宣言が無い動画は gate が REVIEW にする（PASS に倒れない）。
- 既存の final_video を再利用する経路（同じ input_hash でスキップ）では書かない。ENFORCE を ON にする前に描画された動画は
  宣言が無く REVIEW（手動承認の対象）。ON 後に再描画すれば宣言が付く。

### 3. 発話中の声と BGM の差は render で実測する（`BGM_ENABLED` の背後）

`FfmpegSpeechMarginProbe`（ADR-0083 §5。本番と同じ stem の filtergraph を使う）を `SpeechMarginProbe` port 経由で
`RenderActivities` に渡す。**BGM が適用された描画だけ**、描画の後・保存の前に測り、結果を `BgmRenderEvidence.speech` に入れる。
費用は音声のみの 2 本の stem 書き出し + 発話区間ごとの `ebur128`（映像を触らない。完成動画の描画より十分小さい）。
決定論（固定版 ffmpeg・固定 filtergraph・整数の 1/100 LU）。**測れなかったときは描画を止めず `speech=None`**
（品質検査は REVIEW。測定不能を PASS にも BLOCK にもしない）。BGM 無し・測定器が無い（`render_ffmpeg_path` 無し）は測らない。
測定器は `BGM_ENABLED` と `render_ffmpeg_path` が揃うときだけ worker が作る。

### 4. producer は純関数を包むだけ。証跡が読めなければ記録しない（INV-147）

`build_quality_producers(settings)` に 2 つ足す（flag は読まない: 宣言が used のときだけ gate が呼ぶため、OFF では呼ばれない）。

- `CheckKey.BGM`: `load_bgm_evidence`（§1 の key）→ 証跡が無い/読めない/別の動画のもの は **例外**（記録されない = REVIEW）。
  現行カタログ（`${AI_VIDEO_WORK_ROOT}/bgm/catalog.json`）・動画形式（render profile から。不明は `None`）・
  `evidence.speech` を渡して `bgm_quality_checks` を呼ぶ。`evidence_ref` = 証跡の sha256、`now` は gate の時計。
  gate は宣言が used のときだけ呼ぶので、「証跡が無い」は常に異常（宣言との食い違い）として例外にする。
- `CheckKey.MAP`: `geo_evidence_key(ep, 媒体 sha, input_hash)` の `GeoRenderEvidence` を読み、`map_quality_checks` を呼ぶ。
  証跡が無い/読めない/別の動画のものは例外。

API プロセスも同じ `build_quality_service` を使う。API からカタログのディレクトリが読めない構成では、BGM の producer は
例外になり REVIEW（手動承認）に倒れる。これは安全側の失敗で、運用で `AI_VIDEO_WORK_ROOT` を揃える。

### 5. クレジットは同じ読み口から両方の呼び出し元へ（INV-148）

`infrastructure/music/credits.py::BgmCreditSource.credit_lines(episode, 媒体 sha, input_hash)` が証跡の `credit_lines` を返す
（証跡が無ければ `()`）。upload 工程と gate の説明欄 lint の**両方**が `build_upload_metadata(..., credits=...)` にこれを渡す。
説明欄の構成 flag（`AVP_UPLOAD_DESCRIPTION_COMPOSED`）が OFF のときは `build_upload_metadata` が credits を使わないので、
`credit_lines` も**読まない**（MinIO へ触れない。従来と同一）。ON で BGM が無い動画は 1 回の読み取り（無ければ `()`）。
証跡があるのに読めない場合は例外: upload は進まず（再試行）、lint は記録されず REVIEW。

### 6. migration 0019 の追記専用を DELETE / TRUNCATE まで広げる（INV-126 の補足）

0019 は未適用なので編集する（追加の migration にしない）。4 表に DELETE / TRUNCATE 拒否のトリガを足し、episode への外部キーは
`ON DELETE RESTRICT` にする（episode を削除するコードは無い。CASCADE のままでも DELETE 拒否のトリガに当たるので、意図を FK に
書く）。承認 → 報告の複合 FK に `episode_id` を含め、別 episode の報告を承認できない。downgrade は行が無ければ従来どおり
戻せる（トリガを外してから表を落とす）。release 走査器は TRUNCATE の語を破壊的と数えるので、ADR-0081（Accepted・0019 に言及）の
marker で承認する（拒否する側のトリガを作る文であり、データには触れない）。

## 設定キーの grep（AGENTS.md §8）

新しいキーは無い。既存キーの読み手を増やさない（`bgm_enabled` / `geo_maps_enabled` は既存の検査が読み手を固定している）:

- `grep -rn "quality_gate_enforce" apps/ workers/ domain/ infrastructure/ contracts/`: 読み手は `workers/pipeline/run_worker.py`・
  `apps/api/routers/episodes.py`・`infrastructure/quality/wiring.py`・`infrastructure/geo/guard.py`、本 ADR で
  `workers/render/run_worker.py`（writer を渡す。唯一の追加）と `workers/upload/run_worker.py` は読まない。
- producer は flag を読まない。`bgm_enabled` / `geo_maps_enabled` の読み手の集合は変わらない（`tests/architecture` が検査）。

## Alternatives

- **証跡を prefix 列挙で探す**: `ArtifactStore` に列挙を足す契約変更になる。key を結び付けから導く方が小さく、地図と揃う。
- **gate が render の input_hash から「BGM 無し」を逆算する**: font・engine・スレッド数が要り再現できない（ADR-0081）。宣言を読む。
- **producer が宣言を読んで「不使用なら PASS」を返す**: 不使用の PASS は `recorded_checks_provider` が宣言から作る。
  producer が二重に作ると食い違いの余地が増える。producer は used のときの検査だけを持つ。
- **発話余裕を gate の時点で測る**: 作業領域の素材（ナレーション・曲）が既に消えている。render の時点でしか測れない。
- **測れないとき描画を失敗させる**: BGM の入った動画を作れなくなる。REVIEW で人が見られる方が安全で十分。
- **ENFORCE が OFF でも宣言を書く**: OFF の「何も書かない」(INV-130) を壊す。

## Consequences

良い結果:
- すべて ON で、BGM・地図が無い/検査に通る/宣言と記録が揃う Episode は自動で通り、BGM の記録が欠ける・地図が BLOCK・
  動画が差し替わった（sha 不一致）のは止まる。
- 説明欄に載せた BGM クレジットは、lint した文と投稿した文が同一の組み立て口の結果。

悪い結果・引き受けた負債:
- ENFORCE を ON にする前に描画された動画と、ON 後に宣言の書き込みだけ失敗した動画は、再描画するまで REVIEW。
  同じ入力のスキップ経路では宣言を書き直さない（再描画が要る）。
- 発話余裕の閾値は ADR-0083 の暫定校正のまま。実在の曲では再校正が要る。測定は ffmpeg の実行で、極端に長い動画では描画後の
  待ちが増える（音声のみ）。
- API でカタログが読めない構成では、手動投稿の BGM 入り動画は常に REVIEW。
- 旧 key の BGM 証跡は読まれない（実績なし）。
- 隔離環境の integration（実 Postgres のゲート）は `deploy/logging/test/run-e2e.sh` でしか走らせられない。

## ON にする条件の更新（所有者の判断。本 ADR は判断しない）

`AVP_QUALITY_GATE_ENFORCE` の条件 3（render が宣言を書く）が、本 ADR で満たされる。`BGM_ENABLED` / `GEO_MAPS_ENABLED` を ON に
する前に: ENFORCE が ON であること（地図は起動時に強制。BGM は本 ADR 以降 ON を推奨）、worker と API が同じ
`AI_VIDEO_WORK_ROOT` のカタログを読めること、実素材での閾値再校正。
