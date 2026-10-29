# ADR-0083: BGM の承認済みカタログ・scene への配置・ナレーション優先の実測・品質検査

## Status

Proposed（オーナーの承認待ち。番号 0083 と INV-133〜138 は Phase 3 の予約）。
**ADR-0077（Render 結線）を補う。** 0077 の §1（供給元）・§4（混合）・§5（ラウドネス）のうち、下の「0077 との関係」に
書いた項目だけを置き換える。`BGM_ENABLED`（既定 false）の背後で、OFF のとき描画はバイト単位で現行と同じ（INV-113）。

## Context

ADR-0072（契約・選曲・クレジットの純粋ドメイン）と ADR-0077（Render 結線・フラグ・ラウドネス検査）が入ったが、
BGM を**実際に使える**状態にするには次が欠けていた:

1. 曲がカタログに載る手順・保存・Episode をまたぐ再利用が定義されていない（取込の判定 `ingest_check` だけがある）
2. cue sheet を作る者が居ない（`LocalDirectoryBgmSource` は読むだけ。ADR-0077 も「AudioDirector は未実装」）。
   scene の役割（導入・本編・山場・終わり・無音）に応じた配置と、scene をまたいで曲を続ける方法が無い
3. 「ナレーションより BGM が目立たない」は定数（上限 -12 dB・ducking 係数）で、**実音声で測っていない**
   （ADR-0077 の負債。校正が宿題）
4. ラウドネス不合格時の方針が暗黙（例外になる）で、決めた記録が無い
5. 品質ゲート（ADR-0073/0076）が BGM を検査する関数が無い

実データ: 本番 Episode `48f4eddb`（42 秒・Shorts・ナレーション 5 本）の読み取り専用 export で測った。
BGM 無しの完成動画は **-16.5 LUFS / LRA 9.4 / true peak -1.3 dBFS**。再描画（BGM 無し）は元の動画とバイト一致
（サイズ 37,453,047）。台本 scene 5 本の発話はほぼ連続で、隙間が少ない。

## Decision

### 1. 実在の曲は人が供給する。検証用の曲はコードで合成する（INV-133）

- 第三者の音源は取得しない（エージェントはライセンスを確認できない）。**実在の曲を載せられるのは、所有者が音源と
  ライセンスの証拠を渡したときだけ**。手順は §2 末尾。
- 検証用には `tests/support/bgm_synth.py` が標準ライブラリだけで合成する（Am-F-C-G のパッド + 柔らかいキック +
  シェイカー、ステレオ、96 秒、**終端と先頭が連続する**ループ）。契約上は `source_kind="original"` /
  `license_type="custom"`（自作）。契約に `self_generated` 等の新しい語彙は足さない（`contracts/music.py` の語彙を増やすと
  取込・選曲・クレジットの全分岐に波及するため）。ライセンス証拠は「自作・第三者素材なし」の文面の sha256。
  これは**カタログの曲ではなくテスト素材**で、公開用途には使わない。

### 2. カタログ・保存・再利用（INV-133）

- 曲の同一性は**内容の sha256**。原本は `music/originals/<sha256>.<ext>`（content-addressed・不変）、カタログは
  `catalog.json`（`BgmTrack` の配列、`track_id` 昇順）。置き場は ADR-0077 §1 の `${ai_video_work_root}/bgm/`
  （`LocalDirectoryBgmSource` が読む配置）で、書く側は `infrastructure/music/catalog_store.register_track`。
- 「承認済み」とは `license_status="approved"` かつ `license_evidence_sha256`・`license_checked_at` を持つこと。**承認するのは
  人**で、コードは運ぶだけ（`BgmTrack` の契約が承認に証拠を要求する）。登録（再取込）で既存の承認事実は変わらない。
- 同じ sha256 は新規登録せず既存を再利用（`DUPLICATE`）。別の `track_id` で同じ内容を申告しても、新しい承認は生まれない。
  `track_id` が同じで内容が違うものは変造として拒否。原本ファイルが名前と違う内容なら `CatalogStoreError` で止める。
  カタログが壊れていれば上書きせず止める。書き込みは一時ファイル + `fsync` + `rename`。
- **object storage（MinIO）への載せ替えは本 ADR では行わない。** 現状の供給元は作業ディレクトリのファイルで、
  Episode をまたぐ再利用はこのディレクトリが担う。MinIO / DB 版は `BgmSource` port の差し替えで、DDL
  （`bgm_tracks` / `bgm_cue_sheets` / `bgm_usages`）は ADR-0077 末尾の計画どおり別の承認済み枝で採番する。
  **本 ADR は migration を要求しない。**
- 所有者の登録手順: (1) 音源ファイルとライセンス証拠（規約のスクリーンショット/ PDF/ 契約書、購入記録）を用意する、
  (2) 証拠の sha256 を求め、`BgmTrack`（帰属必須なら `exact_attribution_text` を**原文のまま**。説明欄で書き換わる
  `<` `>` 等は契約が拒否する）と `loudness_lufs`（`ebur128` の積分値。配置が必須とする）を埋め、承認を自分で決める、
  (3) `register_track` で登録する。許可プラットフォーム・形式（Shorts 可否）・商用/再利用/改変の可否・期限は
  証拠に書かれたとおりに入れる。迷ったら `unverified` のままにする（選曲に出ない）。

### 3. 選曲（INV-134）

`domain/music/placement.choose_track`: `select_approved`（ライセンスの事実で門を閉じる。INV-97）の結果を、
mood → 直近の使用が少ない → **energy の一致** → カタログ順の安定ソートで並べ、先頭を返す。同じ入力は同じ曲。
法的に通らない曲は他に候補が無くても返さない（`None` = BGM 無しで進める）。

### 4. scene への配置（INV-134）

`domain/music/placement.plan_cue_sheet(plan, track, roles, policy)`（純粋関数）が `RenderPlan` から `MusicCueSheet` を作る。

- scene の役割は `SceneRole`（intro / body / climax / outro / silent）。既定は先頭 intro・末尾 outro・他 body。climax と
  silent は呼び出し側（後続の AudioDirector）が指定する。
- **silent の scene には cue を置かない**。その前後で区間が分かれ、silent の後は曲頭から鳴り直す。
- 連続する非 silent の scene は 1 区間。**同じ曲を scene をまたいで途切れず続ける**ため、`MusicCue` / `BgmMixCue` に
  `source_offset_ms`（既定 0）を足し、cue は区間の頭からの経過位置から鳴らす（ffmpeg は `atrim=start=`）。曲より長ければ
  ループ（合成曲は継ぎ目が連続）。隣り合う cue は境目をまたいで `crossfade_ms`（800 ms）重なり、役割による音量差は線形の
  クロスフェードで滑らかに繋がる。動画の頭は 1500 ms、末尾は 2500 ms でフェード。
- 音量: cue の `target_relative_level_db` は**曲に掛ける gain**。`gain = (声の基準 + 役割の相対値) - 曲の実測ラウドネス`
  （声の基準 = `VOICE_REFERENCE_LUFS_CENTI` = -16.5 LUFS、本番 Episode の BGM 無しの実測）、既存の上限
  `BGM_NARRATION_PRIORITY_MAX_DB`（-12）以下に切る。役割の相対値は intro -13 / body -16 / climax -12 / outro -14（声に対する dB）。
  **曲の `loudness_lufs` が未測定なら配置しない**（推測しない）。上限 -12 は gain に掛かるので、曲が小さい（例: -18 LUFS）と
  役割の差が上限に潰れる。曲は商用マスタ相当（-9〜-15 LUFS）を想定し、登録時に実測値を入れる。
- `BGM_MIX_VERSION` を 2 に上げた（`source_offset_ms` が正準 JSON に入り、ducking の係数を変えたため）。BGM が OFF の
  描画の hash には現れない。

### 5. ナレーション優先: 発話中の声と BGM の差を**測って**判定する（INV-135）

- 測り方（`infrastructure/render/bgm_intelligibility.FfmpegSpeechMarginProbe`）: 本番と同じ filtergraph 部品で
  「ナレーションだけ」「ducking 後の BGM だけ」の 2 本を書き出し（`build_voice_stem_filtergraph` /
  `build_bgm_stem_filtergraph`）、各台本 scene の発話区間で `ebur128` の積分値を測り、差（声 - BGM、1/100 LU）を
  `SpeechIntelligibility` にする。判定は純粋関数 `domain/music/intelligibility`。
- ducking を 弱めた: `sidechaincompress=threshold=0.1:ratio=3:attack=20:release=300`（旧 threshold=0.03 ratio=9）。
  旧設定は実音声で発話中の BGM が声より **約 25 LU 低く**（-40 LUFS 前後）ほぼ聞こえなかった。
- 閾値 `SPEECH_MARGIN_MIN_CENTI_LU` = **12 LU**（`contracts/music_render.py` が唯一の定義）。校正根拠: 本番 Episode の声 5 本 +
  合成曲（-14.9 LUFS）で、新しい設定の発話中の差は **s1 18.0 / s2 20.6 / s3 19.0 / s4 14.8 / s5 16.0 LU**（BGM を載せた完成動画を作った実測。
  詳細は `docs/architecture/v3-phase3-bgm-verification.md`）。最小値 14.8 の下に 約 3 LU の余裕を置いた値。BGM が鳴っていない
  区間は差を持たず不合格にしない。声が測れない区間だけでは合格しない。
- **限界**: 校正は 1 Episode・1 つの合成曲・聴取なし（耳での確認は人がする）。実在の曲を載せたら再校正が要る。
  差が閾値未満なら品質検査（§8）が BLOCK。

### 6. ラウドネス不合格の方針: 失敗のまま（needs_input）にする。BGM 無しへ自動で落とさない（INV-136）

ADR-0077 §5（積分 -24〜-9 LUFS・true peak ≤ -1.0 dBTP の範囲外/無音/実測不能は `FinalVideoValidationError`）を**維持する**。
決めた理由: (a) `FinalVideoValidationError` は永続的な失敗（`NeedsInputError`）で、再試行を空転させない。(b) 混合は決定的なので、
範囲外は cue sheet か曲か声の側の問題で、黙って BGM 無しに落とすと「BGM 付きのはずが無い」事実が運用から見えなくなる
（ライセンス不適合は BGM を使わない方が安全側なので落とす、とは性質が違う）。(c) 運用者の逃げ道は `BGM_ENABLED=false`
（または cue sheet を外す）で、即座に導入前と同じ描画に戻る。実測: 本番 Episode に合成 BGM を足した完成動画は
-16.7 LUFS（LRA 14.6）/ true peak -1.4 dBFS で範囲内。**限界**: true peak の上限（-1.0）は limiter の天井（0.9 ≒ -0.9 dBFS）より内側で、
声そのものが -1.0 dBTP 近くまで来る Episode では BGM の有無に関わらず落ちうる（今回の Episode は声だけで -1.3）。
その場合の扱い（声の正規化）は本 ADR の範囲外。

### 7. BGM だけの変更は render だけを再実行し、fal を 1 回も呼ばない（INV-137）

cue sheet（曲・配置・役割）は production の Artifact・入力 hash・予約台帳に経路が無い（ADR-0077 §2、INV-100/116）。
本 ADR の配置関数も production を import しない（`tests/architecture`）。曲・配置・役割のどれを変えても
render の input_hash だけが変わり、production の成果物（id・版・sha256・入力 hash）と予約台帳（0 件）は不変、
fal クライアントの全メソッドは呼ばれない。

### 8. 品質検査 `bgm_quality_checks`（INV-138）

`domain/music/quality_checks.bgm_quality_checks(...) -> tuple[QualityCheck, ...]`。gate は MUSIC と LICENSE。
BGM を使っていない（証跡なし）なら、**その final_video の sha256 に結び付いた**「使っていない」PASS を MUSIC と
LICENSE の両方で返す。使っているなら、証跡が今の動画のものか・実測ラウドネスが範囲内か（数値から再計算し、保存された
`passed` を信用しない）・発話中の差が閾値以上か（未測定は REVIEW）・曲ごとのライセンスが**今も**有効で音源 sha と証拠 hash が
一致するか・クレジット原文が一致するかを検査し、崩れたら BLOCK。`detail` は固定の ASCII 文で入力文字列を持たない。
`evidence_ref` は証跡オブジェクトの sha256（not used のときは final_video の sha256）。新しい `QualityReason` は足さない
（`AUDIO_DEFECT` / `LICENSE_UNVERIFIED` を使う）。永続化と gate への接続は `claude/v3-p3-quality` の責務。

## 0077 との関係

置き換える: §4 の「ducking の係数」（threshold=0.03 ratio=9 → 0.1 / 3）、`BGM_MIX_VERSION` 1 → 2、cue に `source_offset_ms`。
維持する: §1 の設定キー 1 つ・既定 OFF、§2 同一性、§3 ライセンス不適合は BGM 無しで続行、§5 のラウドネス範囲と失敗の扱い、
§6 証跡、§7 Temporal 互換（Activity の入出力は変えない）。

## 設定キーの grep（AGENTS.md §8）

新しい設定キーは足さない（`BGM_ENABLED` のまま）。新しい**定数**の置き場: `SPEECH_MARGIN_MIN_CENTI_LU` /
`VOICE_REFERENCE_LUFS_CENTI` は `contracts/music_render.py` のみ（`tests/architecture/test_bgm_phase3_boundaries.py` が
他での再定義を検査）。役割の音量・フェード長は `domain/music/placement.PlacementPolicy` のみ。
実装前に `grep -rn "source_offset_ms\|SPEECH_MARGIN\|VOICE_REFERENCE_LUFS\|SceneRole" apps/ workers/ domain/ infrastructure/ contracts/ docs/`
を走らせた: ヒット 0 件（読み手 0 / 書き手 0 / 無関係 0）。

## Alternatives

- **実在のフリー音源を取得して検証する**: ライセンスをエージェントが確認できない。権利不明の曲で作った検証動画は証拠にならない
- **`source_kind` に `self_generated` を足す**: 取込・選曲・クレジットの全分岐に波及する語彙の追加。自作は `original` / `custom` で表せる
- **scene ごとに曲頭から鳴らす（offset 無し）**: scene 境目ごとに曲が鳴り直す。聴感で最も目立つ欠陥
- **volume の時間変化式で役割ごとの音量を付ける**: filtergraph の組み方が複雑になり、cue の契約（数値は int・cue が単位）から外れる。
  クロスフェードする cue の重なりで足りる
- **ラウドネス不合格で BGM 無しへ自動フォールバック**: 上記 §6。BGM 付きのはずが無いことが運用から見えなくなる。
  ライセンス不適合と違い安全側の理由が無い
- **ducking を強いまま（旧設定）にして上限を上げる**: 発話中だけ極端に下がり、隙間で音が膨らむ「ポンピング」が増える
- **聞き取りやすさを `loudnorm` の補正で担保**: 2 パスで決定性が落ちる（ADR-0077 と同じ理由）
- **MinIO / DB へ今すぐ載せ替える**: DDL が要る。別の承認済み枝（番号は他が持つ）。供給元は port なので後から差し替えられる

## Consequences

良い結果:
- 曲の登録（重複排除・変造検知）・選曲・配置・実測・検査が揃い、`BGM_ENABLED` を ON にしたとき cue sheet を作る関数がある
- scene をまたいで曲が途切れず、silent の scene は無音。役割で山場が持ち上がる
- ナレーション優先を数値（発話中の差）で言え、閾値の根拠が実音声の実測にある
- BGM を替えても fal 0 回・production 不変を fake provider で検査できる

悪い結果・引き受けた負債:
- **聴取による確認は人がしていない**。数値と波形の proxy のみ（抜粋 wav を添えた）。主観の「聞こえ方」は所有者が確認する
- 閾値・役割の音量・ducking の係数は 1 Episode・合成曲 1 つの校正。実在の曲で再校正が要る
- cue sheet を**作って置く呼び出し元は未配線**（`plan_cue_sheet` は純粋関数。AudioDirector か運用スクリプトが呼ぶ）。
  発話中の差の実測も Activity には配線していない（`FfmpegSpeechMarginProbe` は存在するが呼び出しは検証スクリプトのみ）。
  そのため品質検査は差が未測定だと REVIEW になる（fail-closed）
  **→ ADR-0086 で結線済み**: render が BGM 適用時に測り証跡の `speech` に入れる。測れなければ従来どおり REVIEW
- 供給元は作業ディレクトリのファイル。ホストの故障で消える（バックアップ対象に含めるかは運用判断）
- 声そのものの true peak が -1.0 dBTP に近い Episode では BGM 付き描画が落ちうる（§6）
- 説明欄へのクレジット記載の実配線・投稿前に「今の動画の曲は有効か」を再確認する Gate は未配線（ADR-0077 から継続）
