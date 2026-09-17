# Render Worker の運用

Status: **Accepted (2026-09-14)**（ADR-0019）

> **常駐運用は compose**（`docker compose --profile core up -d`、`docs/operations/workers.md` / ADR-0024）。
> 以下の host プロセス方式はデバッグ用の代替。同じ queue に両方を同時に立てないこと。

現行の `production_manifest` から完成動画（`final_video`）を描き、技術検査に合格したものだけを保存して
Episode を `render_ready` に置く worker。固定版の static ffmpeg を子プロセスで使うため、
**Ubuntu ホストのプロセス**として動かす（Temporal / PostgreSQL / MinIO は compose 側）。

## 描画エンジンの導入（初回のみ）

```bash
./scripts/install-render-ffmpeg.sh
```

固定イメージ `mwader/static-ffmpeg:7.1.1@sha256:11a44711684c0b9f754c047dcd64235b8b52deab251bd0e0a86f22faa160749c`
から `$HOME/.local/share/avp/ffmpeg/7.1.1/{ffmpeg,ffprobe}` を取り出し、sha256 を照合する。

| バイナリ | sha256 |
|---|---|
| ffmpeg | `810f94020e76e2b58fb44759a322e86bea5d213ebededad7471f3a15b0bf2c5c` |
| ffprobe | `4818b8964b5d7b699370628a4154c97e88205678ee506ca72e9330600e917667` |

worker は**起動時に** ffmpeg の存在・実行権・sha256・版を検証し、合わなければ Temporal に接続する前に
エラーログを出して終了する（`RENDER_FFMPEG_PATH / RENDER_FFMPEG_SHA256 are not set` /
`ffmpeg binary sha256 mismatch`）。

## 起動

```bash
docker compose --profile core up -d --wait
export RENDER_FFMPEG_SHA256=810f94020e76e2b58fb44759a322e86bea5d213ebededad7471f3a15b0bf2c5c
./scripts/run-render-worker.sh        # 別ターミナル
```

1プロセスが2つの Worker を持つ: task queue `render`（RenderWorkflow・状態系 Activity、通常の並行枠）と
`render-media`（描画 Activity だけ、並行数 `RENDER_CONCURRENCY`）。長い描画中も他 Episode の入場・失敗記録は待たされない。

環境変数（未設定なら script / `infrastructure/config.py` が既定値を入れる。既定値の宣言元は
`contracts/render.py` の `DEFAULT_RENDER_*`）:

| 変数 | 既定 | 備考 |
|---|---|---|
| `RENDER_FFMPEG_PATH` | `$HOME/.local/share/avp/ffmpeg/7.1.1/ffmpeg` | 絶対パス |
| `RENDER_FFMPEG_SHA256` | （必須） | 不一致なら起動しない |
| `RENDER_FFMPEG_THREADS` | 4 | input_hash に入る（変えると同じ入力でも再描画） |
| `RENDER_FONT_PATH` / `RENDER_FONT_SHA256` | Noto Sans CJK Regular | 描画ごとに sha256 を照合。不一致は `RenderEngineUnavailableError`。sha256 は input_hash に入る |
| `RENDER_CONCURRENCY` | 1 | `render-media` Worker の `max_concurrent_activities`（同時描画数） |
| `RENDER_TIMEOUT_SECONDS` | 1800 | エンジンの時間切れ。**worker 側の値**を admit が workflow へ返す（API は渡さない）。Activity の start_to_close = これ + 300秒 + 2秒 ×（profile の最長尺の秒数） |
| `RENDER_MIN_FREE_BYTES` | 10 GiB | 事前検査: 空き ≥ これ + 入力量 × 4 |
| `AI_VIDEO_WORK_ROOT` | `/mnt/minio-hdd/ai-video-work` | 作業領域（`work-directories.md`）。試行ごとに `episodes/<ep>/<job>/attempt-<n>/` を作る。成功と cancel では片付け、**失敗では調査のため残す**（下記） |

## 実行・確認

```bash
curl -X POST http://localhost:8000/episodes/<episode_id>/render                      # 既定 profile（shorts_vertical）
curl -X POST http://localhost:8000/episodes/<episode_id>/render \
  -H 'content-type: application/json' -d '{"render_profile_id": "long_form_horizontal"}'
curl http://localhost:8000/episodes/<episode_id>                                     # render_ready / final_video
```

smoke の確認点:

1. Episode が `render_ready`、job `render_final_video` が `succeeded`
2. `final_video` の JSON を MinIO から読み戻し、sha256 が `artifact_metadata.sha256` と一致・契約（`parse_final_video`）で読める
3. `media.object_key`（`media/<ep>/final_video/<sha256>.mp4`）の読み戻し sha256 が `media.sha256` と一致
4. 同じ POST をもう一度 → job が `skipped`、同じ Artifact（描画しない / INV-17）
5. 別 profile で POST → `version` が増え、前の版に `superseded_at`

## 状態と対処

| 症状 | 原因 | 対処 |
|---|---|---|
| POST が 404 / 422 | Episode が無い / 未登録の `render_profile_id` | id を確認（`RENDER_PROFILES`） |
| POST が 409 | 入れない状態（`assets_ready` / `render_ready` / render 自身の `needs_work`・`blocked` 以外）、または同じ Episode の render workflow が実行中 | 状態を確認 / 実行の終了を待つ |
| POST は 202 だが何も起きない | admit が拒否（他工程が止めた `blocked`、走っている run が `in_progress`） | Episode の `workflow_id`（入場トークン）を確認 |
| `blocked`、`RenderInputMissingError` | 現行マニフェスト・参照 Artifact・メディア本体が無い | production を再実行 |
| `blocked`、`RenderInputStaleError` | マニフェストの参照が現行でない（素材が作り直された） | production を再実行してマニフェストを組み直す |
| `failed`、`RenderInputIntegrityError` | 保存物の sha256 不一致・契約違反 | MinIO / artifact_metadata を調査（ADR-0019 の負債: permanent） |
| `blocked`、`DurationReconciliationError` / `VoiceTimelineOverflowError` | シーン動画が短すぎる / 音声が重なる | 素材か storyboard を直す |
| `blocked`、`RenderEngineUnavailableError` | フォントが無い・sha256 不一致（ffmpeg は起動時に検査） | 導入・設定し直して POST（再開） |
| `blocked`（`RETRY_BUDGET_EXHAUSTED`）、`RenderEngineFailedError` / `RenderEngineTimeoutError` / `FinalVideoCorruptError` / heartbeat timeout | 3回描画して失敗 | worker ログ・作業領域の空きを確認して POST（再開） |
| `blocked`、`RenderWorkspaceFullError` | 空き容量不足（事前検査 / ENOSPC）。**自動削除しない** | `AI_VIDEO_WORK_ROOT` の空きを作って POST |
| `blocked`、`FinalVideoValidationError` / `UnknownRenderProfileError` | 解像度・codec・音声欠落など決定的な検査不合格 | エンジン・profile の不整合を調査 |
| workflow cancel 後 `blocked` | 描画の子プロセスを止め、作業領域を片付けてから needs_input で記録（production と同じ） | POST で再開 |

## 失敗時の作業領域の片付け（運用者）

描画・検査が失敗した試行の作業領域（`input/` 素材、`tmp/` エンジンのログ・字幕、`output/` 出力）は残す。
worker は `render failed; keeping work directory <path> (episode=... job=... attempt=...)` を警告ログに出す。

- 調査が済んだら、その試行のディレクトリだけを消す: `rm -rf "$AI_VIDEO_WORK_ROOT/episodes/<ep>/<job>/attempt-<n>"`
- **worker が同じ job を実行中のときは消さない**（Temporal UI で workflow `episode-<ep>-render` が閉じていることを確認）
- 自動削除はしない（`work-directories.md` の方針。空き不足は `RenderWorkspaceFullError` で止まる）

## 既知の制約

- 同時描画は既定1本（`RENDER_CONCURRENCY`）。長尺を連続で流すとキューが詰まる（状態系は別 queue なので止まらない）
- 素材・完成動画の取り出し・保存・読み戻しは 8MiB ずつ流す。作業領域には入力と出力の実体が要る（事前検査 = 入力量 × 4 + 最低空き）
