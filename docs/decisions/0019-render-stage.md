# ADR-0019: Render 工程（完成動画）と Episode `render_ready`

## Status

Accepted (2026-09-14)

## Context

Phase 4（ADR-0017）は Episode を `assets_ready` に駐機させる: storyboard シーンごとの画像・動画、
台本シーンごとの音声、それらを束ねた `production_manifest`。次に要るのは、それを1本の完成動画
（`final_video`）にまとめる工程である。

- 前身repoではレンダーの失敗原因を追えなかった（素材の版と完成動画の対応が失われた。artifact.md）
- 素材は 9:16 だが、出力は Shorts（縦）だけとは限らない。解像度・縦横比を**コードに埋め込むと**
  横長の長尺を足すたびに検査規則とフィルタが割れる
- シーン動画の実尺は生成 provider の都合で storyboard の尺と一致しない（ADR-0017 §6 で ±max(300ms, 5%) を許容）。
  合わせ方を決めないと、黙った速度変更やループで品質が落ちる
- 描画はローカル計算で課金は無いが、数分〜数十分 CPU とディスクを占有し、cancel と heartbeat が要る
- 次工程（Phase 6 Upload）はまだ無いので、Episode は完成動画の後で**実際に駐機する**（ADR-0011 の基準）

## Decision

**固定した描画エンジンで、出力 profile と時間軸ポリシーから決定的に組んだ描画計画を描き、技術検査に
合格した完成動画だけを `final_video` として保存し、Episode を駐機点 `render_ready` に置く。**

### 1. 描画エンジン

- 固定版の静的バイナリを子プロセスで呼ぶ（版・イメージ digest・sha256 は `docs/operations/render-worker.md`）。
  バイナリの sha256 を使用前に検証し、`RenderEngineIdentity(engine, version, binary_sha256)` として
  input_hash と完成動画に記録する
- 実測（probe / 技術検査）は in-process のデコーダを使う
- 字幕フォントはパス + sha256 で設定し、sha256 を input_hash に入れる
- **domain はエンジンの名前を知らない**（`domain/render/ports.py` の `RenderEngine` port）

### 2. 契約（`contracts/render.py`）

- `RenderProfile`: `profile_id` / `width` / `height` / `fps_millis` / `fit`（cover|contain）/
  `video`（h264, crf, preset, yuv420p）/ `audio`（aac, bitrate, 48kHz, 2ch）/ `subtitles`（配置・行長・行数）/
  `limits`（最短・最長・尺の許容差・音声必須）。**縦横比は幅と高さから導出**し保存しない
- 組み込み `RENDER_PROFILES`: `shorts_vertical`（1080x1920, cover, 最長3分）と
  `long_form_horizontal`（1920x1080, contain = 9:16 素材の左右を黒で埋める, 最長3時間）。
  既定は `DEFAULT_RENDER_PROFILE_ID = "shorts_vertical"`。未知の id は contracts で `KeyError`、
  domain / worker で `UnknownRenderProfileError`、API では 4xx
- production の生成 profile と render profile は別物（素材を作り直さずに出力だけ変えられる）
- `TimelinePolicy`: `max_freeze_ms`（既定 2000）/ `transition = "cut"`（5A はカットのみ）/
  `template_version`（`RENDER_TEMPLATE_VERSION`。計画・描画の意味を変えたら上げる）
- `RenderPlan`: `scenes`（時間軸上の開始・尺・要求尺・実尺・合わせ方）/ `voices`（開始・尺）/
  `subtitle_cues`（**台本ナレーションへの文字オフセットだけ**。本文を複製しない）/ `total_duration_ms` /
  profile・policy・engine のスナップショット。**別 Artifact として保存しない**。固定入力からの純粋関数なので
  再計算で再現でき、正準 JSON の sha256 を `final_video.render_plan_sha256` に残す
- `FinalVideoArtifact`（`ArtifactType.FINAL_VIDEO`、Episode 単位で `scene_id` は NULL）: 入力3件の固定
  （manifest / script / storyboard）、profile・policy・engine・template_version のスナップショット、
  `media`（mp4、上限 `FINAL_VIDEO_MAX_BYTES` = 8GiB。シーン素材の 25MB 上限とは別）、`measured`（実測）、
  `timeline` / `voice_placements` / `subtitle_cues`（計画の複写）、`technical_qa`（`passed` は常に true）
- モデル自身が内部整合性を検査する: 時間軸が 0 から隙間・重なりなく続き合計が総尺と一致、音声は重ならず総尺内、
  cue は順序どおり重ならず自分の音声の窓の中、実測の解像度 = profile、実測の尺 = 総尺 ± 許容差、
  音声必須なら音声あり、技術検査の全項目合格
- 旧予定の `edit_decisions` は採らない（描画計画が置き換える）。創作面の品質判定 `review_report` は Phase 5B

### 3. 時間軸（`domain/render/timeline.py`）

- 時間軸 = storyboard のシーン順と storyboard の尺（開始は累積）
- シーン動画の実尺 A と目標 T: A = T → `exact`、A > T → `trim`（先頭 T ms を使う）、
  A < T かつ T − A ≤ `max_freeze_ms` → `freeze_tail`（最終フレームを保持）、それ以外 → `DurationReconciliationError`
- **速度変更・ループ・黙った品質低下はしない**
- 音声は台本シーンが参照する最初の storyboard シーンの開始に置く。次の音声と重なれば
  `VoiceTimelineOverflowError`。最後の音声が総尺を `max_freeze_ms` 以内で超える場合だけ最終シーンの
  freeze を延ばして総尺を延長する

### 4. 音声・字幕

- 5A はナレーション音声だけ（BGM / 効果音は無し）。BGM にはライセンス付きの入力 Artifact と
  ライセンス・クレジット・ダッキングのメタデータが要るので**別 ADR**
- 無音の土台に各音声を profile のサンプルレート・ch へ揃えて置き、正規化・ラウドネス補正なしで合算する（決定性）
- 字幕の真実は台本ナレーション。文単位に分け、行長 × 行数で折り返し、音声の窓内に文字数比例で割り付ける。
  字幕ファイルは描画時に作業領域で台本から具体化する。`subtitles.enabled=false` を許す

### 5. 同一性と冪等性

`render_input_hash = sha256(canonical_json({stage: "render", manifest / script / storyboard の sha256,
profile 全体, policy, template_version, engine identity, font_sha256,
audio_mix（混合規則: サンプルレート・ch・利得・正規化なし・版。`domain/render/audio.py`）}))`。試行・job・run・時刻・パスは含めない。
同じ input_hash の現行 `final_video` があれば描画せず job を `skipped` にする（INV-17）。違えば新しい version、
旧版は `superseded_at`（ADR-0012）。

### 6. 技術検査（`domain/render/qa.py`、純粋関数）

デコード可能・解像度・縦横比・fps・尺（計画 ± 許容差、profile の最短最長）・h264 / yuv420p・音声（aac, rate, ch）・
バイト数・シーンの網羅と連続・音声の網羅・cue の範囲・入力 sha256 の読み戻し一致・保存した本体の読み戻し sha256。
**合格したときだけ保存する。**

### 7. 状態と DB

- `EpisodeStatus.RENDER_READY`、`JobType.RENDER_FINAL_VIDEO`、`ArtifactType.FINAL_VIDEO`（migration 0005 で
  CHECK を張り替え、downgrade は Phase 4 の語彙を literal で凍結）
- 遷移: `in_progress --RENDER_READY--> render_ready`、`render_ready --STAGE_ADMITTED--> in_progress`（再描画・Phase 6 の入口）
- 入場（`RENDER_ADMISSIBLE_STATUSES`）: `assets_ready` / `render_ready`（`STAGE_ADMITTED`）、render 自身が止めた
  `needs_work`（`RETRY_ADMITTED`）/ `blocked`（`RESUMED`）。判定の権威は admit Activity、トークンの規則は ADR-0017 §8 と同じ
- ローカル計算なので予約台帳に載せない（ADR-0017 §5 と同じ理由）。`ProviderCall` は増えない

### 8. Temporal

- `RENDER_WORKFLOW = ("RenderWorkflow", "render")`、workflow id `episode-{id}-render`、task queue `render`
  （`RENDER_TASK_QUEUE`）。Activity 名と入出力は `contracts/render_activities.py`
- admit → `render_final_video`（単一の重い Activity）→ mark_ready、失敗時は record_failure
- 描画 Activity: start_to_close `DEFAULT_RENDER_TIMEOUT_SECONDS`（30分）、heartbeat timeout 60秒、
  heartbeat は10秒以内ごと、retry 最大3回（retryable の型だけ）、`WAIT_CANCELLATION_COMPLETED`。
  cancel では子プロセスグループへ終了要求 → 5秒 → 強制終了 → 作業領域を片付けて再送出
- API: `POST /episodes/{id}/render`（任意で `render_profile_id`）

### 9. 失敗クラス

| 例外 | クラス | 事象 |
|---|---|---|
| `RenderInputMissingError` | `needs_input` | 現行のマニフェスト・参照 Artifact が PG / MinIO に無い |
| `RenderInputStaleError` | `needs_input` | マニフェストの参照が現行でない |
| `RenderInputIntegrityError` | `permanent` | 入力の sha256 不一致・契約違反 |
| `RenderSourceMediaError` | `needs_input` | 素材メディアが読めない・未対応形式・尺の食い違い |
| `DurationReconciliationError` | `needs_input` | シーンの尺を合わせられない |
| `VoiceTimelineOverflowError` | `needs_input` | 音声の重なり・総尺超過 |
| `RenderEngineFailedError` / `RenderEngineTimeoutError` | `retryable` | 非zero終了・シグナル・時間切れ |
| `RenderWorkspaceFullError` | `retryable` | 空き容量不足（事前検査 / ENOSPC）。自動削除しない |
| `RenderEngineUnavailableError` | `needs_input` | バイナリ・フォントが無い / sha256 不一致（運用者が導入すれば回復） |
| `FinalVideoValidationError` | `permanent` | 解像度・codec・音声欠落など決定的な技術検査の不合格 |
| `FinalVideoCorruptError` | `retryable` | デコード不能・読み戻し sha256 不一致 |
| `UnknownRenderProfileError` | `permanent` | 未登録の profile id |

retryable を使い切ったら `blocked`（`RETRY_BUDGET_EXHAUSTED`）で、terminal にしない。cancel は失敗ではない。

### 10. 作業領域・並行数・既定値

作業領域は `WorkDirectory`（`episodes/<ep>/<job>/`）。事前検査: 空き ≥ `DEFAULT_RENDER_MIN_FREE_BYTES`（10GiB）+ 入力量 × 4。
既定値の唯一の宣言元は `contracts/render.py` の `DEFAULT_RENDER_*`（並行数 1、timeout 1800秒、heartbeat 60秒、
空き 10GiB、freeze 2000ms、スレッド 4）。検査: `tests/unit/test_render_vocabulary.py::test_render_defaults_are_declared_exactly_once`。

## Alternatives

**(a) Remotion（ブラウザ描画）** — レイアウトの表現力が高い。しかし Chromium の描画は環境で揺れて決定性が無く、
企業利用のライセンスが要り、依存が重く、cancel が粗い。却下。

**(b) OpenMontage の描画を実行時に使う** — 既製の編集パイプライン。AGPL で、固定した版の外部実装に完成動画の
意味を委ねることになる。却下（読み取り専用の参照に留める）。

**(c) 描画計画を別 Artifact（`edit_decisions`）として保存する** — 計画を直接見られる。しかし固定入力からの
純粋関数なので再計算で再現でき、保存すると「保存した計画」と「再計算した計画」の食い違いという新しい失敗が増える。
sha256 だけ残す。却下。

**(d) 尺の不一致を速度変更・ループで吸収する** — 失敗が減る。しかし黙って映像の意味が変わる（口の動き・動作の速さ）。
needs_input で人間に返す。却下。

**(e) 9:16 を固定し、横長は後で考える** — 実装が単純。しかし解像度が検査規則・字幕配置・フィルタに散り、
追加時に全箇所を直すことになる。profile で最初から分ける。却下。

**(f) `render_ready` を足さず `ready_for_review` へ進める** — 本番パイプラインの正常系は `ready_for_review`
を通る（ADR-0006）。しかし承認の対象（投稿メタデータ・創作面の品質判定）がまだ無く、人間に何を承認させるかが
決まらない。駐機点を足す。却下。

## Consequences

**良い側**
- 完成動画が入力の版・profile・エンジンの sha256・計画の sha256 を持つので、描画失敗・見た目の差を再現できる
- 横長の長尺を profile の追加だけで出せる（契約・検査・字幕配置が profile を読む）
- 同じ入力の再実行は描画せず素通りする（INV-17）

**悪い側 / 引き受けた負債**
- **ADR-0011 の駐機点がさらに増えた**（`script_ready` / `storyboard_ready` / `assets_ready` / `render_ready`）。
  パイプラインを1本の workflow に畳むときに再評価する
- `permanent`（`RenderInputIntegrityError` / `FinalVideoValidationError` / `UnknownRenderProfileError`）は Episode を
  terminal `failed` にする。入力の破損は production の再実行で直りうるので failure-policy §2 の条件3と緊張がある。
  「保存済みの内容・設定は決定的に同じ失敗を返す」を根拠に permanent を採り、運用で頻発したら needs_input へ見直す
- `render_ready` からの再描画が失敗したとき、現行の `final_video` は旧 profile のまま残る（ADR-0015 と同じ負債）
- production の入場規則（`PRODUCTION_ADMISSIBLE_STATUSES`）に `render_ready` を足していない。描画後に素材を
  作り直すには Phase 5 では手段が無い
- 字幕は文字数比例の割り付けで、発話のタイミングとはずれうる（音声認識による整列は将来）
- BGM・効果音・カット以外のトランジション・創作面の品質判定は無い（Phase 5B / 別 ADR）
- 描画は1本ずつ（並行数1）。長尺ではキューが詰まる

## 陳腐化条件

- BGM / 効果音を入れるとき（ライセンス付き入力 Artifact の ADR）
- Phase 6 Upload が `render_ready` の後に同じ workflow で続くようになったとき（駐機点の再評価）
- 描画エンジンを差し替える、または GPU 描画を入れるとき（決定性と identity の再定義）
