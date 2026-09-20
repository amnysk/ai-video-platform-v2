# ADR-0028: 音声の実尺を描画の前に区間へ合わせる

## Status

Accepted (2026-09-20)

## Context

2026-09-19 の初の en-US Episode（`87bbf7de`）は、制作（有料の画像・動画）まで進んでから描画で
`VoiceTimelineOverflowError: voice s1 ends at 10147 ms, after voice s2 starts at 7000 ms` になり、
5 シーン中 5 つで音声が区間を超えていた（実測は ADR-0026 追補）。ADR-0026 は台本と storyboard に
**推定**の読み上げ予算（語/秒）を課して再発を減らしたが、追補自身が書くとおり予算は保証ではない
（句読点の間・声で速度が変わる）。保証が無いまま次の順に並んでいる:

1. 台本・storyboard（LLM、予算は推定）
2. 制作: 音声（Piper、実尺が初めて分かる）と、**並行して**画像・動画（fal.ai、有料）
3. 描画: `place_voices` が「前の音声の終わり <= 次の音声の始まり」を初めて判定する

つまり実尺が分かる 2 の時点では誰も区間と突き合わせず、溢れは有料工程の後の 3 で見つかる。
描画は `needs_input` で正しく止まるが、止まった時点で課金は済んでいる。また稼働中の
production / render worker は 2026-09-17 のイメージ（86c9ea9a）で、ADR-0026 の予算検査を含む
版（script / storyboard = 64fb067a）とは別だった。

## Decision

**合成後の実尺を、描画が使う区間と同じ窓へ、有料工程の前に、決定論的に合わせる。描画側は緩めない。**

1. **区間の唯一の定義は既存の `domain.storyboard.coverage.script_scene_spans`**（描画の `place_voices` と
   同じ置き方。最後の台本シーンは storyboard の終端まで）。新しい尺の定数は持たない。
   content / render profile（Shorts・long_form）に依らず、storyboard の並びだけで決まる
2. **音声 Activity が合成後に判定する**（`workers/production_voice/activities.py`、判定は純粋関数
   `domain/production/voice_fit.py`）。実尺 <= 区間ならそのまま。超えたら話速を上げて合成し直す:
   - 生成器が `SpeedAdjustableVoiceGenerator`（`synthesize_at_speed`、Piper は `length_scale` を倍率で割る）を
     持つときだけ。倍率は「区間の 97% に収まる」量（`VOICE_FIT_HEADROOM_PERMILLE = 30`）を今の話速に掛けて決める
   - 再合成は最大 `VOICE_FIT_MAX_RESYNTHESES = 2` 回、話速は等速の `MAX_VOICE_SPEEDUP_PERMILLE = 1250`
     （1.25 倍）まで。**上限を超える調整は丸めず** `VoiceExceedsSceneSpanError`
   - 速さを変える生成器を持たない場合も同じエラー（再合成しない）
   - 実際に使った話速は Artifact の `generator.generation_profile_id` に `+fit<permille>` で残す
     （スキーマは変えない。等速の音声と別の生成として区別できる）。`voice_input_hash` は基の profile のまま
     （同じ入力 → 同じ調整、INV-17）
3. **`VoiceExceedsSceneSpanError` は `needs_input`**（`VoiceTimelineOverflowError` の下位型）。
   同じ入力で再実行しても同じ結果になる欠陥なので retryable にしない。台本 / storyboard を
   作り直す（人間の判断、ADR-0014 の LLM 欠陥の扱いは上流工程の責務のまま）
4. **音声を有料メディアより先に済ませる**（`ProductionWorkflow._produce`）。音声の枝が全部成功するまで
   画像・動画を起動しない。音声が失敗したら有料の submit は 1 件も走らない。音声は非課金・ローカルで
   処理は数秒のため、待ち時間の増加は小さい。既存の実行履歴の再生を壊さないよう `workflow.patched` で入れる
5. **最終の契約検査はマニフェスト組み立てに置く**（`ProductionActivities._assemble`）。現行の全音声の実尺を
   同じ関数 `check_voices_fit_spans` で区間と突き合わせ、違反を全件まとめて `VoiceExceedsSceneSpanError` にする。
   検査を持たない版の worker が作った音声・storyboard だけが差し替わった世代の音声を、描画へ渡さない。
6. **描画（`place_voices` の重なり判定・最終シーンの `max_freeze_ms`）は変えない。** 最後の砦のままで、
   ここに到達するのは 2〜5 が破れたときだけ

優先順位への対応: (1) 台本が尺を意識 = ADR-0026（既存、重複実装しない）→ (2) 合成後に実尺を取得 = 2 →
(3) 描画前に調整 = 2 の話速 → (4) 最終の契約検査 = 5（→ 6）。文章短縮・尺の再計算は行わない（下記）。

## Alternatives

- **描画側で溢れを無視・切り詰める**: 音声の途中切れ・次の音声との重なりを黙って出す。ADR-0019 が禁じる
- **音声の実尺に合わせて storyboard のシーン尺を伸ばす**: 動画はシーン尺で生成済み（有料）。伸ばせるのは
  最終シーンの freeze だけ（ADR-0019 §4）で、途中のシーンは伸ばせない。storyboard を作り直すと動画も作り直しになる
- **溢れたら LLM に文章を短縮させて台本から作り直す**: 上流 Artifact（台本 → storyboard → 制作）の連鎖を
  この Activity が壊す。課金済みの成果物を無効にしうる。責務の分離（INV-4）に反する。
  上流には ADR-0026 の予算検査がすでにあり、そこで再生成される
- **話速の上限なしで必ず収める**: 聞き取れない音声になる。品質を黙って落とさず、失敗にする
- **音声と有料メディアを並行のまま、失敗時に兄弟を cancel する（従来）**: 音声の合成より画像の submit が先に
  走りうる。cancel は provider 側のジョブを止めない（ADR-0017 §4）ので課金が残る
- **ADR-0026 の rate を下げて余裕を増やす**: 推定である限り保証にならない。有用だが別問題

## Consequences

- 良い: 溢れる Episode は、課金前（音声合成の直後）に、原因のシーンと数値つきで止まる。9/19 型
  （実測 1.97〜2.72 語/秒で 8 秒枠に 10.6 秒）は、最大 1.25 倍の調整で収まるものは収まり、収まらないものは
  有料の前に blocked になる
- 良い: 1.25 倍以内の超過は人手なしで直る。ja-JP・long_form・別 profile は区間が storyboard から決まるので
  定数の書き換えが要らない
- 悪い: **話速は聞こえ方を変える**。1.25 倍は「聞き取れる上限」の判断で、耳での確認はしていない
  （上限は `next_speedup_permille(max_permille=...)` で生成器・言語ごとに変えられる形にしてある）。
  実運用で不自然なら 1000〜1250 の間で下げる（下げるほど needs_input が増える）
- 悪い: 音声が先に済むので、制作全体の壁時計は音声の合成時間（`voice_concurrency` 既定 1 で直列）だけ延びる
- 悪い: Piper 以外の `VoiceGenerator`（将来の TTS）は `synthesize_at_speed` を実装するまで調整できず、
  超過は即 `needs_input` になる
- **負債**: `MAX_VOICE_SPEEDUP_PERMILLE` は 1 つの値。locale / voice ごとに持つのは、ADR-0026 の負債
  （locale → voice の対応表）と一緒に解く
- **負債**: 最後の台本シーンの区間は描画の freeze 延長（`max_freeze_ms`）を余裕に数えない。
  描画が許す範囲より保守的で、超過を許す方向の緩和は render profile を音声 Activity に渡す設計が要る
- 稼働中の production / voice / render worker は、この変更を含む版へ入れ替えるまで従来の挙動
  （描画で初めて溢れを検出）。イメージの統一は運用手順の別件
