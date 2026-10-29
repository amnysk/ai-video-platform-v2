# BGM 実素材検証（Phase 3 / ADR-0083）

本番 Episode `48f4eddb-5924-4d03-add0-c705eb41f5f0`（Shorts・42 秒・ナレーション 5 本・storyboard 10 本）の
**読み取り専用 export** を、本番の外で本番の描画経路に通した記録。再現: `tests/support/bgm_verify_export.py`
（`uv run --extra dev python -m tests.support.bgm_verify_export EXPORT_DIR OUT_DIR`）。

## 経路（本番と同じ部分 / 違う部分）

- 同じ: `RenderActivities.render_final_video`（現行マニフェストの読み込み・`build_render_plan`・`render_input_hash`・
  固定版 ffmpeg 7.1.1 `FfmpegRenderEngine`・`PillowAvMediaProbe`・技術 QA・`FfmpegLoudnessProbe`・BGM 証跡の保存）、
  `LocalDirectoryBgmSource`、`resolve_bgm`（ライセンスの再検査）。
- 違う: 保存先は SQLite（メモリ）+ メモリ ArtifactStore + 一時ディレクトリ。PostgreSQL / MinIO / Temporal / fal には
  接続しない（avp2-* のコンテナ・本番ストレージには触れていない）。artifact の id・sha256 は export のまま投入した。
- 曲は `tests/support/bgm_synth.py` が合成した 96 秒のループ（-14.9 LUFS、`source_kind=original` / `license_type=custom`、
  ライセンス証拠は「自作・第三者素材なし」の文面）。第三者の音源は使っていない。

## 結果（出力: `scratchpad/bgm-verify/`）

| 項目 | 元の本番動画 | BGM 無し（再描画） | BGM 有り | BGM 有り（配置だけ変更: s3 無音・s4 body） |
|---|---|---|---|---|
| 積分ラウドネス | -16.5 LUFS | -16.5 | **-16.7** | -16.6 |
| LRA | 9.4 LU | 9.4 | 14.6 | 12.2 |
| true peak | -1.3 dBFS | -1.3 | **-1.4** | -1.4 |
| 尺（format / video / audio） | 42.0 / 42.0 / 42.0 s | 同じ | 同じ | 同じ |
| 音声・映像の start_time | 0 / 0 | 0 / 0 | 0 / 0 | 0 / 0 |

- BGM 無しの再描画は元の本番動画とバイト数が一致（37,453,047）= 描画は決定的。BGM 有りの映像ストリームの md5 は BGM 無しと一致
  （映像は BGM に触れない）。AV の同期は尺・start_time とも一致。
- 積分値が BGM で少し**下がり** LRA が増えるのは、ナレーションの隙間（ゲートで除外されていた静かな区間）に曲が入ってゲートを
  通るため。ナレーション自体は変わっていない（声だけの stem は -16.4 LUFS）。
- BGM 有りの 3 本はいずれも `bgm_loudness`（-24〜-9 LUFS・true peak ≤ -1.0 dBTP）に合格。

### 発話中の声と BGM（ducking 後）

| scene | 声 (LUFS) | BGM (LUFS) | 差 (LU) | 役割 |
|---|---|---|---|---|
| s1 | -16.2 | -34.2 | 18.0 | intro |
| s2 | -15.6 | -36.2 | 20.6 | body |
| s3 | -16.8 | -35.8 | 19.0 | body |
| s4 | -16.0 | -30.8 | **14.8**（最小） | climax |
| s5 | -17.0 | -33.0 | 16.0 | outro |

閾値 12 LU（`SPEECH_MARGIN_MIN_CENTI_LU`）に対し全 scene 合格（余裕 2.8 LU）。発話の無い区間の BGM は
-29.9〜-33.7 LUFS で、発話中（-30.8〜-36.2）より 2〜5 LU 高い（ducking が効いている）。

### 調整の経緯（正直な記録）

1. 既存の ducking（threshold=0.03 ratio=9）+ 役割の音量では、発話中の BGM が声より **約 25 LU 低く**（-40 LUFS 前後）、
   ほぼ聞こえなかった。2. ducking を `threshold=0.1 ratio=3` に弱め、役割の音量を曲の実測ラウドネスで補正する方式にした
   （ADR-0083 §4/§5）。3. 合成曲のキックが強く crest が高かったため、-18 → -14.9 LUFS に（商用マスタ相当）。
   4. limiter の天井（0.9 → 0.8）も試したが、この声（-1.2 dBFS 付近）では出力が変わらなかったため**元の 0.9 のまま**。

### BGM だけの変更

BGM 有り → 配置変更の 2 回目も fal 呼び出し 0 回・予約台帳 0 件・production の artifact（id・sha256）不変・
render の input_hash は 3 本とも別（BGM 無し `7536770c…` / BGM 有り `04cc39ea…` / 配置変更）。

## 品質の正直な評価

- **耳では聞いていない**（私は音を聴けない）。数値と波形から: BGM は声の 15〜21 LU 下の「床」として鳴り、声を覆わない。
  この差は明瞭度には十分だが、**BGM の存在感はかなり控えめ**（スマホのスピーカーでは、ほぼ聞こえない可能性がある）。
  もう少し聞かせたいなら役割の音量を上げ、閾値の余裕（2.8 LU）の範囲で調整する。聴取による最終判断は所有者。
- 曲は合成パッド + 柔らかい打楽器で、**音楽としての出来は検証用**。ループの継ぎ目は連続（単体テスト）だが、
  情緒・ジャンルの適合は実在の曲でないと評価できない。
- ナレーションの途中で BGM が膨らむ「ポンピング」: ducking の release=300 ms。この Episode は声がほぼ連続なので
  顕著ではない。間の多い Episode では聞こえ方が変わりうる（未確認）。
- 校正は 1 Episode・1 曲。実在の曲を載せたら閾値・音量を再確認する。

## 出力ファイル（`scratchpad/bgm-verify/`）

`episode_with_bgm.mp4` / `episode_without_bgm.mp4` / `episode_with_bgm_alt_placement.mp4`、
`excerpt_15s_with_bgm.wav` / `excerpt_15s_without_bgm.wav`（ナレーションが最も詰まった 15 秒＝ 6.0〜21.0 秒、発話 9.4 秒）、
`ebur128_{original,without_bgm,with_bgm}.txt`、`report.json`、`synth_track.wav`、`license_evidence.txt`、`stems/`（声・BGM の stem）。
