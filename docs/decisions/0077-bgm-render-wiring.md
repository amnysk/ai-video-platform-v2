# ADR-0077: BGM の Render 結線（1 つの設定キーの背後・既定 OFF・ffmpeg で混合）

## Status

Proposed（オーナーの承認待ち。番号 0077 と INV-113〜116 は Phase 2-E の予約）。
**既定は OFF**で、OFF の間の挙動はバイト単位で現行と同じ（INV-113）。本番へ入れる判断は別途。

## Context

ADR-0072 は BGM の契約・選曲・クレジットの純粋ドメインを入れたが、Render（ADR-0019）には結線していない。
設計 v3 §4.3 は Remotion へ cue sheet を渡す前提だが、ADR-0019 は Remotion を採らず ffmpeg で描く。
本 ADR はこの食い違いを**ffmpeg 側に合わせて**解く（設計側を現実に合わせる。ADR-0072 の宿題）。

守るもの（監査 R5、設計 T12〜T16・T27）:
権利の確認できない曲を使わない / BGM だけの変更で fal（高額）を呼ばない / 本番の挙動を黙って変えない /
ナレーションより BGM が目立たない / 音量が規格外の動画を黙って通さない。

## Decision

### 1. 設定キーは 1 つ: `Settings.bgm_enabled`（環境変数 `BGM_ENABLED`、既定 `false`）

他の設定キーは足さない。カタログと cue sheet の置き場は `${ai_video_work_root}/bgm/`（固定の相対名）で、
DDL を使わない非 DB の供給元（JSON ファイル + 音源ファイル）。供給元は port（`BgmSource`）で、後続の枝が DB / MinIO 版へ
差し替える。flag が OFF、または cue sheet が無い Episode では、BGM の経路は**一切走らない**。

### 2. 同一性（INV-113）

- `render_input_hash(..., bgm=None)` は `bgm` キーを**入れない**。BGM を実際に混ぜるときだけ `BgmMixPlan` の正準 JSON
  （cue ごとの source_sha256・license_evidence_hash・時刻・フェード・上限適用後の dB・ducking・混合の版）を入れる。
- `build_filtergraph` / `build_argv` は `bgm=None` で現行と同一の文字列・argv を返す。BGM の経路は別の関数で組む。
- production の入力 hash・fal 予約・scene Artifact には BGM を渡す経路が無い（INV-116）。

### 3. ライセンス不適合の扱い（INV-114）: BGM 無しで描画を続ける（拒否しない）

cue sheet が指す曲が 1 つでも「未承認・失効・取消・隔離・再利用/商用/改変不可・Shorts 等の形式不可・期限切れ・
カタログに無い・実ファイルの sha256 が違う/読めない・証拠 hash 不一致・帰属文不一致・cue が時間軸の外/未知の scene」
に当たれば、**BGM 全体を使わない**（一部の cue だけ鳴る動画は演出を壊すので全部か無しか）。

- 描画は BGM 無しの入力で続ける（input_hash も ffmpeg コマンドも BGM 無しと同一）。理由コードをログに残す。
- 「拒否して Episode を止める」案は採らない: BGM は付加価値で、素材は有効。権利の問題で日次が止まるより、
  権利の問題がある曲を**使わない**ことが安全側。使われなかった事実は証跡と運用ログに残るので黙殺ではない。
- 期限は描画のたびに `now` で再検査する。すでに BGM 入りの現行 final_video があっても、曲が失効していれば
  input_hash が BGM 無しの値になり再利用されず、BGM 無しで描き直される。
- 残る穴: 投稿前に「今この final_video の曲は有効か」を再確認する Gate は未配線（INV-104 と同じ境界）。

### 4. ffmpeg での混合（ADR-0019 の audio 規則を拡張）

- BGM の入力は scene 動画 → ナレーション → cue の順（cue ごとに 1 入力。尺が足りなければ `-stream_loop -1`）。
- cue ごと: 声を基準 0 とした dB（`min(cue, BGM_NARRATION_PRIORITY_MAX_DB=-12)`）→ フェードイン/アウト → 開始位置へ
  遅延 → 総尺まで無音で延長。`ducking_policy=duck_under_voice` の cue は `sidechaincompress`（ナレーションバスを
  サイドチェーン）で声の間だけ下げる。最後に `alimiter` で天井を抑え、総尺で切る。
- ducking のパラメータは `infrastructure/render/ffmpeg_graph.py` の定数で、スナップショットテストが固定する。
  変えるときは `BGM_MIX_VERSION`（`contracts/music_render.py`）を上げる（input_hash に入り再描画される）。

### 5. ラウドネス検査（INV-115）

BGM を混ぜた完成動画だけ、保存前に ffmpeg の `ebur128`（`peak=true`）で実測し、数値（1/100 LUFS・1/100 dBTP の int）と
理由コード（`ok` / `too_quiet` / `too_loud` / `true_peak_exceeded` / `silent`）を持つ構造化結果にする。
技術 QA の 1 項目（`bgm_loudness`）として評価し、範囲外・無音・実測不能は `FinalVideoValidationError` /
再試行可能な失敗になる。**黙って通さない**。許容範囲は `contracts/music_render.py` が唯一の定義で、
**実素材で校正した値ではない**（積分 -24〜-9 LUFS、true peak ≤ -1.0 dBTP は暫定）。

### 6. 証跡とクレジット

BGM を使った完成動画ごとに `BgmRenderEvidence`（episode / input_hash / final_video sha256 / 曲ごとの source sha256 と
license_evidence_hash / 実測ラウドネス / `credit_lines`）を MinIO に JSON として保存する（DDL 無し・新しい ArtifactType 無し・
キーは `artifacts/{episode}/bgm_render_evidence/{sha}.json`）。保存は final_video の行を記録する**前**で、
証跡の無い BGM 入り動画は存在しない。`credit_lines` はカタログの原文（Wave 1 の `credits_for`）で、説明欄の agent は
これを読む。説明欄に収まるかの BLOCK（`fit_credits`）は説明欄側の責務。

### 7. Temporal 履歴との互換（INV-116）

Activity の追加・順序変更・入出力 dataclass の変更をしない。`RenderFinalVideoRequest` / `RenderFinalVideoResult` は
不変。BGM の供給元とラウドネス測定は `RenderActivities` のコンストラクタ引数（任意、既定 None）で、Activity の入力ではない。
workflow は変更しない。

## 設定キーの grep（AGENTS.md §8）

`grep -rn "bgm_enabled\|BGM_ENABLED" apps/ workers/ domain/ infrastructure/ contracts/ docs/` を実装前に走らせた:
ヒット 0 件（読み手 0 / 書き手 0 / 無関係 0）。実装後に同じ grep を走らせた内訳（コード 3 件 + 配布設定 2 件 + 文書）:

| 場所 | 分類 | 件数 |
|---|---|---|
| `infrastructure/config.py`（`Settings.bgm_enabled`） | 定義 | 1 |
| `workers/render/run_worker.py` | 読み手（worker 組み立て。唯一） | 1 |
| `contracts/music_render.py`（`BGM_SETTING_KEY`） | キー名の唯一の定義 | 1 |
| `compose.yaml` / `.env.example`（`BGM_ENABLED`） | 書き手（配布設定。既定 false） | 2 |
| `docs/` | 無関係（説明） | — |

読み手が 1 箇所であることは `tests/architecture/test_bgm_render_boundaries.py::test_the_setting_key_is_read_only_where_the_worker_is_assembled` が検査する。

## DDL が必要になる場合の計画（本 ADR では書かない）

カタログと cue sheet を DB に置くときは、`bgm_tracks`（`BgmTrack` の列）・`bgm_cue_sheets`（episode_id, sheet JSON, sha256）・
`bgm_usages`（`BgmUsage`）を新設する。ロールバックは 3 表の `DROP`（他表への FK を張らない設計のため単独で戻せる）。
いずれも別の承認済み枝で採番する。

## Alternatives

- **Remotion に cue sheet を渡す（設計 v3 の記述どおり）**: ADR-0019 が Remotion を却下している。第二の描画系を足さない
- **ライセンス不適合なら Render を拒否して Episode を止める**: 付加価値のために有効な素材の日次が止まる。使わないことが
  安全側で、証跡に残せば足りる
- **一部の cue だけ使う**: 曲が途中から消える動画は演出を壊し、どの cue が落ちたかの推測が増える
- **cue sheet / BGM の有無を ArtifactType にして manifest に入れる**: production の input_hash に絡み、
  BGM だけの変更が fal 再生成を誘発しうる（ADR-0072 が退けた案と同じ）
- **ducking を volume 自動化の式で組む**: 声の位置が実測に依存し、音声の長さ変更に追従できない。sidechaincompress は
  声の実波形に追従する
- **完成動画 JSON に BGM フィールドを足す**: `FinalVideoArtifact` の契約変更で OFF のときの出力にも影響しうる。
  別オブジェクトなら OFF のとき何も増えない
- **ラウドネス補正（loudnorm）を混合に入れる**: 2 パスが要り決定性が落ちる。まず測って落とす（補正は校正後に再検討）

## Consequences

良い結果:
- OFF のとき本番の入力 hash・ffmpeg コマンド・成果物が変わらない（ゴールデンで固定）
- BGM の変更は render だけを再実行し、production 側に経路が無い
- 権利の確認できない曲は他に曲が無くても鳴らず、鳴らさなかった理由が機械可読

悪い結果・引き受けた負債:
- **ラウドネスの許容範囲・BGM 上限 -12 dB・ducking の係数は未校正**。実素材での聴取と測定で校正する
- ducking を使う cue が多いとナレーションバスの分岐が増える（描画時間は未測定）
- 供給元は暫定のファイル。cue sheet を作る側（AudioDirector）は未実装で、現状 flag を ON にしても cue sheet を
  置く人がいなければ何も起きない
- 投稿前に曲の有効性を再確認する Gate、説明欄へのクレジット記載の実配線は未実装
- 同一 `final_video` の再利用判定は描画時の `now` 基準で、描画後に失効した曲は次の描画まで検出されない
- INV-41〜96 は他の枝の予約。番号は再利用しない
