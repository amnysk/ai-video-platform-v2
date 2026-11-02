# speech-margin 測定の ffmpeg 固まり（music stem の未消費枝）

## 症状
BGM 試聴ツール（`scripts/bgm_preview.py`）が Episode 48f4eddb で `bgm speech margin not measured
(RenderEngineFailedError)` を 3 曲中 1 曲、2 回の実行とも出した（本番なら品質検査は REVIEW）。

## 原因（実測）
- timeout の不足ではない。music stem の ffmpeg が **300 秒の timeout まで戻らない**（通常 0.5 秒）。
  再実行で捕えた例外: `bgm stem measurement could not run: Command [...] timed out after 300 seconds`、
  途中の出力は 7,864,320 B（完成時 8,064,078 B。EOF 約 1 秒前で停止）。
- 曲の中身に依らない。同一入力・無負荷・固定版 ffmpeg 7.1.1 で stem を 150 回作り直すと **14 回固まった**
  （別の 150 回で 7 回）。`-t` で出力を切っても同率（7/150）。負荷は必須ではない。
- `build_bgm_stem_filtergraph` が声の mix を `asplit` して 1 本を `[vmix]anullsink` に捨てていた。
  誰も引かない枝が詰まり、sidechaincompress 側が飢えて EOF 手前で停止する（タイミング依存）。
- 枝を除いた graph（`asplit` を sidechain の数だけにする）は 200 回中 0 回、出力 wav は旧 graph の成功時と
  sha256 が同一。本番の最終 mix（`[vmix]` を amix が引く）は影響を受けない。

## 固定するテスト
| テスト | 防ぐ失敗 |
|---|---|
| `test_the_music_stem_has_no_unconsumed_branch_that_can_deadlock_ffmpeg` | stem に `anullsink` / 使われない `asplit` 枝が戻り、測定が確率的に固まる |
| `test_a_unmeasurable_margin_keeps_the_render_and_leaves_the_evidence_unmeasured`（追記） | 警告が型名だけで、timeout か ffmpeg の異常終了かが後から分からない |

既存テスト `test_the_music_stem_reuses_the_real_bgm_chain...` は `"[vmix]anullsink" in stem` を外した
（原因の枝そのものを廃止したため。cue ごとの鎖が本番と同一であることの検査は変えていない）。

## 残る注意
timeout（`STEM_TIMEOUT_SECONDS`=300）は据え置き。通常の所要は 1 秒未満で、固まりは待っても戻らない。
測れないときは従来どおり `speech=None`（REVIEW）。
