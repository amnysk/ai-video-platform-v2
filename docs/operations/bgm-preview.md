# BGM 試聴ツール（`scripts/bgm_preview.py`）

実在の曲を載せた動画を**本番の外で描いて、耳で聴く**ためのツール。本番の描画経路
（`RenderActivities` + 固定版 ffmpeg 7.1.1 + 本物の `FfmpegSpeechMarginProbe`、ADR-0083）をそのまま使い、
保存先だけを SQLite（メモリ）+ メモリ ArtifactStore + 一時ディレクトリに置き換える。Phase 3 の
実素材検証（`docs/architecture/v3-phase3-bgm-verification.md`）の再利用で、設計の変更ではない。

## 安全の約束

- 本番の PostgreSQL / MinIO / Temporal / fal / YouTube / LLM には**つながない**。`render` はプロセス内の
  TCP 接続と DNS 解決を遮断して走り、終了時に fal / production worker のモジュールが 1 つでも読まれていたら失敗する。
- 出力は**すべて** `PREVIEW_ONLY_NOT_FOR_UPLOAD__` で始まるファイル名。mp4 には metadata の透かし
  （`title` / `comment` = `PREVIEW ONLY - NOT FOR UPLOAD`）が入る。**YouTube へ上げない**。
- ライセンス未承認の曲は `--allow-unapproved` を付けたときだけ使える。承認扱いにするのは
  **scratch に作る複製カタログの中だけ**で、元のカタログは読むだけ。何を変えたかは report の
  `license.overridden_for_preview_only` に残る。本番の `resolve_bgm` / `plan_cue_sheet` は未承認の曲を
  引き続き拒否する（テストで固定）。本番の既定値（ducking・役割の音量・閾値）は変えない。
- 出力先が export 元・MinIO データ領域と重なる場合は止まる。

## 使い方

`make bgm-preview ARGS="..."`（= `PYTHONPATH=. python scripts/bgm_preview.py ...`。venv の python を `PYTHON=` で渡す）。

```bash
# 1. （任意）本番 Episode を読み取り専用で export。SELECT と GET だけ。.env の接続先を使う。
make bgm-preview PYTHON=.venv/bin/python ARGS="export <episode_id> /path/to/export"

# 2. カタログ（catalog.json + music/originals/<sha>.<ext>）と曲 id を指定して描く
make bgm-preview PYTHON=.venv/bin/python ARGS="render /path/to/export \
  --catalog /path/to/catalog --track bgm-pd-shika-no-tone --track bgm-cc0-ambient-ck61 \
  --out /path/to/out --level-offsets=-3,0,3 --allow-unapproved"

# 動作確認用: 合成トーンの曲だけのカタログ（第三者の音源なし。既定は未承認）
make bgm-preview PYTHON=.venv/bin/python ARGS="synth-catalog /path/to/catalog"
```

主なオプション: `--ffmpeg`（既定 `~/.local/share/avp/ffmpeg/7.1.1/ffmpeg`、sha256 を検証）、
`--profile`（既定 `shorts_vertical`）、`--roles s1=intro,s3=silent,s4=climax`（scene の役割）、
`--level-offsets`（cue の音量を一律にずらす dB のリスト。**報告用の掃引**で、本番既定は変えない）。
export は `script/ storyboard/ production_manifest/ scene_video/ scene_voice/` の JSON と
`media/<episode_id>/...` を持つディレクトリ（`tests/support/bgm_verify_export.py` と同じ形）。

1 回の描画は Episode（42 秒）で 1 variant あたり 1 分前後。曲数 × offset 数だけ繰り返す。

## 出力と聴き方

```
OUT/
  PREVIEW_ONLY_NOT_FOR_UPLOAD__<ep8>__without_bgm.mp4          # A: BGM 無し
  PREVIEW_ONLY_NOT_FOR_UPLOAD__<ep8>__without_bgm__phone.wav   # A のスマホ再生シミュレーション
  <track_id>/offset_+0dB/
    PREVIEW_ONLY_NOT_FOR_UPLOAD__<ep8>__<track_id>__offset_+0dB.mp4        # B: BGM 有り
    ..._phone.wav                                                          # B のスマホ再生シミュレーション
    report.json / report.md / stems/（voice.wav, music.wav = ducking 後）
  summary.json                                                 # 全 variant の比較表
```

1. `without_bgm.mp4` と BGM 有りの mp4 を**同じ音量で**交互に聴く（A/B）。
2. `*_phone.wav` はスマホのスピーカー相当（300 Hz 以下を切り、7 kHz 以上を落とし、モノラル）。
   「実際の視聴環境で BGM が聞こえるか / 声が埋もれないか」はここで判断する。ざっくりの近似で、実機ではない。
3. `--level-offsets=-3,0,3` の 3 本を聴き比べ、好みの位置を決める。決めた値は人が役割の音量の設計に反映する（このツールは書き換えない）。

## 数字の読み方（report.md / summary.json）

| 項目 | 意味 |
|---|---|
| integrated LUFS | 動画全体の音量。BGM 無しからの差は小さいのが普通（隙間に BGM が入ると僅かに下がり LRA が増える） |
| true peak | -1.0 dBFS 以下が目安（BGM 有りの動画の検査 `bgm_loudness` と同じ） |
| min / median margin (LU) | 発話中の（声 - BGM）。大きいほど声が聞き取りやすい。閾値 12 LU 未満は FAIL。大きすぎる（20 超）と BGM はほぼ聞こえない |
| BGM in gaps (LUFS) | 発話の無い区間の BGM の音量。発話中より 2〜5 LU 高ければ ducking が効いている |
| sync | 尺・音声映像の start_time の差が 40 ms 以内なら OK |
| license.production_would_reject | 本番なら拒否される理由（例 `license_not_approved`）。承認前の曲を試聴していることの記録 |

最終判断は耳で行う。数字は「声を覆っていないか」の下限を保証するだけで、曲の出来・雰囲気の適合は保証しない。
mix cue の level は `-12 dB`（`BGM_NARRATION_PRIORITY_MAX_DB`）で頭打ちになる。静かな曲では役割間の音量差が
潰れうるので、report の `mix_levels_db` を見る。

## 実測の記録（2026-11-02、Episode 48f4eddb・offset 0、未承認候補曲を `--allow-unapproved` で）

BGM 無し: -16.5 LUFS / TP -1.3 / LRA 9.4。出力: `~/.local/share/avp2/bgm-preview-20261102/out/`。

| 曲 | cue level (dB) | 積分 LUFS | TP | 発話中の差 min / median (LU) | 隙間の BGM (LUFS) |
|---|---|---|---|---|---|
| 鹿の遠音（尺八） | -14,-17,-17,-17,-15 | -16.7 | -1.3 | 14.6 / 25.7 | -41.2 |
| ck61（CC0 ambient） | -15,-18,-18,-18,-16 | -16.7 | -1.6 | 18.2 / 20.7 | -34.5 |
| eastern thought（-19.9 LUFS） | -12,-13,-13,-13,-12 | -16.7 | -1.4 | 19.6 / 21.4 | -37.5 |

全て閾値 12 LU に合格、尺の同期 OK。**-12 dB の頭打ち**は eastern の intro/outro に掛かる（役割差 5 dB が
-12/-13 の 1 dB に潰れる）。数値上は margin 19.6 LU と十分なので安全側に倒れるだけで、問題は「役割で音量が変わる
演出が出ない」こと。聴いて intro/outro の盛り上がりが足りなければ `--level-offsets` では解決しない（頭打ちの
後に効かない）ため、頭打ちの扱いは別途設計判断。鹿の遠音は LRA が大きい曲で median 25.7 LU とほぼ聞こえない区間がある。

注意: 描画中の本番側の発話マージン測定が variant のうち 1 本で `bgm speech margin not measured ...
RenderEngineFailedError` の警告を出すことがあった（2 回の実行で各 1 回、原因は未調査。再現手順: 同じコマンドの
標準エラーを見る）。このツールのレポートは別に stem を測るので数値は出るが、本番で起きれば品質検査は REVIEW になる。
