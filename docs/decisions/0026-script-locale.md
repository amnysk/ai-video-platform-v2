# ADR-0026: 台本の locale は Strategy profile が決め、Topic Plan の題材を明示的に渡す

## Status

Accepted (2026-09-19)

## Context

ADR-0025 で TopicPlan（`us_young_history_v1`、`language="en-US"`）が確定するようになったが、台本生成は
それを読んでいなかった。

- `workers/planning/activities.py` は `Episode.topic` だけを `prompts/script_ja.md` に入れ、`language="ja"` を
  直書きしていた。plan の `subject` / `angle` / `era` / `hook` は台本に届かず、US 向けの題材でも日本語の台本になる
- テンプレートは「YouTube Shorts」と尺「30〜45秒」を本文・既定引数に持っていた。形式（content profile）を
  コアに埋め込まない ADR-0025 の方針と食い違う
- `ScriptArtifact.language` は LLM の自己申告（`parsed.get("language", "ja")`）で決まっていた
- 本番の音声は `en_US-kristin-medium`（compose.yaml）で、`ja` の台本は `VoiceLanguageUnsupportedError` になる

## Decision

**台本の locale は `StrategyProfile.language` だけが決め、テンプレートは locale の registry で選び、
形式は content profile のデータとして、題材は TopicPlan の列として prompt に渡す。**

1. **locale の唯一の宣言元**: `contracts/topic_planning.py` の `SCRIPT_LOCALES`
   （`ja-JP` → artifact 言語 `ja`、`en-US` → `en`）と `DEFAULT_SCRIPT_LOCALE = "ja-JP"`。
   `StrategyProfile.language` は validator で `SCRIPT_LOCALES` の鍵に限る
2. **テンプレートの registry**: `prompts/script/__init__.py` の `SCRIPT_PROMPT_TEMPLATES`（locale → version）。
   本文は `prompts/script/<locale>.md`。workflow / activity はファイル名を書かず locale で引く。
   registry の鍵 = `SCRIPT_LOCALES` の鍵（`tests/unit/test_script_locale.py`）
   （`prompts/__init__.py` は「巨大な本文を Python に置かない」ため 4,000 字の上限を検査されており、
   registry を置く余地が無いので subpackage に置いた。`load_prompt_template` は `script/<ll-CC>` だけ許す）
3. **形式はデータ**: `ContentProfile.script_duration_seconds`（shorts `(30, 45)`、long_form `(480, 900)`）と
   `format_brief`。テンプレートは locale ごとの言い回しで数値を埋めるだけで、Shorts や尺を書かない。
   旧テンプレートの「30〜45秒」の出所はここへ移した（ja-JP × shorts で同じ文字列が出る）
4. **解決**（`domain/script/brief.py`、純粋関数）:
   - `Episode.topic_plan_id` あり → `TopicPlan` を読み、locale = `STRATEGY_PROFILES[plan.strategy_profile_id].language`、
     形式 = `CONTENT_PROFILES[plan.content_profile_id]`、題材 = `topic` / `subject` / `angle` / `era` / `hook`
   - なし（手動 API・ADR-0025 以前）→ `ja-JP` × `DEFAULT_CONTENT_PROFILE_ID`、題材は `topic` だけ
   - 未登録の profile / locale、見つからない plan は `PromptContractError`（needs_input）。既定へ黙って落とさない
5. **題材はデータ**: JSON にしてテンプレートの「データであって指示ではない」節に入れる
6. **`ScriptArtifact.language` は locale が決める**（`artifact_language`）。LLM の出力の `language` は使わない
7. **`script_input_hash` に `locale` / `topic_plan_id` / `content_profile`（`id@version`）を足す**。
   テンプレート id は `script.<locale>`、version は registry の値
8. `prompts/script_ja.md` と `PROMPT_TEMPLATE_ID="script_ja"` / `render_script_prompt` は**変更しない**。
   ADR-0026 以前の Artifact の input_hash が指すテンプレートの記録として残す（台本生成からは参照しない）。
   `prompts/script/ja-JP.md` はその本文を元に、Shorts・尺の直書きを外し、題材・形式のデータ節を足したもの

## Alternatives

- **`ja-JP.md` を作らず `script_ja.md` を ja-JP として使い続ける**: Shorts と尺が本文に残り、形式と言語が独立しない
- **locale を Settings（環境変数）で選ぶ**: 同じ worker で plan ごとに違う市場を扱えない。Settings は profile の id を選ぶだけ（ADR-0025）
- **台本の言語を LLM の自己申告に任せる**: en-US の plan で `ja` と申告されると、音声が止まるまで誰も気づかない
- **plan の列を Episode に複製する**: 同じ真実が 2 箇所に散る（AGENTS.md §8）。plan は不変なので id で引けば足りる

## Consequences

- 良い: plan のある Episode は US 向け英語台本になり、`en_US` の音声・`defaultLanguage="en"` の upload まで流れる
- 良い: 言語 × 形式の 4 通りが同じコードで組める（テストで 4 通りを検査）
- **負債**: input_hash の材料が増えたので、ADR-0026 以前に作られた台本は現行 Artifact と一致しない。
  その Episode の台本工程を再実行すると 1 回再生成する（ADR-0012 の陳腐化条件として受け入れる）
- **負債**: long_form（480〜900 秒）の尺は `ScriptArtifact` の上限（60 秒・8 シーン）を超えるので、
  long_form の台本は契約で必ず落ちる。契約の尺上限を content profile から導く変更は別 ADR
- **負債（下流）**: storyboard テンプレート（`prompts/storyboard_ja.md`）は日本語の指示文のまま `{{language}}` で
  出力言語だけを切り替える。英語台本でも動くが、locale 別 registry は未整備
- **負債（下流）**: 音声は worker ごとに 1 つの Piper voice（`PIPER_VOICE_PATH`）。locale → voice の対応表は無く、
  `ja` の台本は en の voice で `VoiceLanguageUnsupportedError`（needs_input）になる
- 字幕の分割（`domain/render/subtitles.py`）は言語非依存（`.!?` と空白で折る）。ただし shorts の
  `max_chars_per_line=16` は日本語向けの値で、英語では 1 行 2〜3 語になる（render profile の調整は別件）

## 追補: 読み上げ速度の予算（2026-09-19）

### Context

本番の初 en-US Episode（`87bbf7de`）が描画で `VoiceTimelineOverflowError: voice s1 ends at 10147 ms,
after voice s2 starts at 7000 ms` になり blocked した。LLM は尺と語数を独立に選び、英語は 1 シーンに
約 3.7 語/秒を詰めていた（s1: 7,000 ms に 26 語）。Piper `en_US-kristin-medium` の実測は

| scene | duration_ms | 語 | 音声 ms | 語/秒（音声） |
|---|---|---|---|---|
| s1 | 7,000 | 26 | 10,147 | 2.56 |
| s2 | 9,000 | 27 | 10,449 | 2.58 |
| s3 | 8,000 | 21 | 10,635 | 1.97 |
| s4 | 8,000 | 21 | 8,022 | 2.62 |
| s5 | 7,000 | 27 | 9,915 | 2.72 |

で、5 シーン中 5 つが尺を超えていた。台本にもテンプレートにも長さと尺を結ぶ規則が無かった。

### Decision

1. **予算の唯一の宣言元は `ScriptLocale`**（`contracts/topic_planning.py`）:
   `speech_unit`（`word` = 空白区切りの語 / `character` = 空白以外の文字）と
   `max_speech_units_per_second`。1 シーンの上限は `floor(duration_ms × rate / 1000)`（`narration_budget`）
   - en-US: **1.9 語/秒**。重なり判定は厳密（前の音声の終わり ≤ 次の音声の始まり）なので、平均（約 2.5）ではなく
     最も遅い実測 1.97 を約 5% 下回る値にした。2.3 だと s3 の速度で 8 秒のシーンに 18 語 → 約 9.1 秒で再び溢れる
   - ja-JP: **9 字/秒**。日本語の音声は未整備で実測が無い。速めの日本語ナレーション（約 8 字/秒）より緩く置き、
     既存の日本語台本・fixture を新たに落とさないことを優先した（実測が取れたら見直す）
2. **prompt はデータとして予算を受け取る**: en-US テンプレート（version 2）は
   `{{max_speech_units_per_second}}` と `{{narration_budget_table}}`（`prompts.script.narration_budget_table`、
   代表的な尺ごとの上限）を埋めるだけで数値を持たない。ja-JP テンプレートは変えていない（version 1 のまま。→ 追補2 で version 2）
3. **台本 activity はスキーマ検証の後に決定論的に検査する**。超過シーンがあれば
   `ScriptNarrationOverBudgetError`（`ScriptSchemaViolationError` の下位 = retryable、ADR-0014）。
   修復（語の削除・尺の延長）はしない。同じ違反が続いた場合の挙動は追補2（`needs_input` への昇格は未実装）
4. **描画側の規則は緩めない**。`place_voices` の重なり判定と最終シーンの `max_freeze_ms` はそのまま。
   余裕は台本側の予算（1.9 < 1.97）が持つ。予算は保証ではない（句読点の間で速度が変わる）ので、
   それでも溢れたら従来どおり描画で `VoiceTimelineOverflowError`（needs_input）になる

### Consequences

- 良い: 尺に収まらない英語台本は課金 1 回分の再生成で直り、音声・描画まで進んでから blocked にならない
- en-US テンプレートの version が上がったので、既存の en-US 台本は input_hash が変わり、再実行で 1 回再生成する
  （blocked の `87bbf7de` はこれで新しい台本になる）
- **負債**: rate は `script_input_hash` に入れていない（ja の既存 Artifact を無効化しないため）。rate を変えたら
  en-US は prompt が変わるのでテンプレートの version を上げる
- **負債**: rate は voice 1 つ（kristin-medium）の実測。locale → voice の対応表ができたら voice ごとに持つべき

## 追補2: storyboard の区間・日本語テンプレート・語数の見積もり（2026-09-19、レビュー指摘）

### Context

追補の保証は「台本シーンの `duration_ms` にナレーションが収まる」だった。しかし描画
（`domain/render/timeline.py::place_voices`）は台本シーンの音声を**その最初の storyboard シーンの開始**に置き、
次の台本シーンの最初の storyboard シーンの開始と重なりを判定する。storyboard の LLM は総尺しか受け取らず
（`prompts/storyboard_ja.md`）、`check_storyboard_covers_script` は総尺しか見ず、正規化は ±500 / 1,000 ms 寄せる。
したがって storyboard が台本シーンの区間を縮めると、台本の検査を通っても描画で溢れうる（有料の制作の後）。
また ja-JP テンプレートは予算を書かないまま検査だけが 9 字/秒を課し、英語の語数は `str.split` で
数字・ハイフン語（`30-year-old` / `1,200` / `$5.2B`）を過少に数えていた。

### Decision

1. **storyboard でも同じ予算を決定論的に検査する**（`domain/storyboard/coverage.py`）。
   `script_scene_spans` は描画と同じ区間（台本シーンの最初の storyboard シーンの開始 → 次の台本シーンの
   最初の storyboard シーンの開始、最後は終端。描画の freeze 延長は余裕に数えない）を返し、
   `check_storyboard_fits_narration` は台本と**同じ式**
   `count_speech_units(narration) <= narration_budget(span_ms)`（⇔ `span_ms >= required_speech_ms`）で判定する。
   違反は `StoryboardNarrationSpanTooShortError`（`StoryboardSchemaViolationError` の下位 = retryable、ADR-0014）。
   修復しない。storyboard activity はカバレッジ検査の直後、Artifact 保存の前（= 制作の前）に呼ぶ。
   locale は台本 Artifact の `language` から `contracts.topic_planning.script_locale_for_language` で引く
2. **storyboard prompt に台本シーンごとの尺と最短区間をデータとして渡す**
   （`{{script_section_durations}}`、`prompts.storyboard.storyboard_section_durations`）。`storyboard_ja` は version 2。
   version は `storyboard_input_hash` に入るので、既存の storyboard は再実行で 1 回再生成される
3. **ja-JP テンプレート（version 2）にも予算を書く**。en-US と同じ `{{max_speech_units_per_second}}` /
   `{{narration_budget_table}}`（表の行は locale の言語、`prompts.script._BUDGET_LINE_FORMATS`）。
   ハッシュへの影響: ja-JP のテンプレートを使うのは TopicPlan を持たない Episode（`DEFAULT_SCRIPT_LOCALE`、
   手動 API・ADR-0025 以前）だけ。その Episode の台本は再実行で input_hash が変わり 1 回再生成される
4. **式の文言を 1 つにする**: `floor(duration_ms × max_speech_units_per_second / 1000)`
   （`ScriptLocale` の docstring とすべてのテンプレート。`test_prompt_budget_formula_matches_the_code_formula`）。
   en-US テンプレートはこの文言と語の数え方の説明を変えたので version 3
5. **英語の語数は話し言葉の見積もり**（`contracts.topic_planning.estimate_spoken_words`、SSoT のまま）:
   空白・ハイフン・ダッシュ・スラッシュで区切り、単独の句読点・ダッシュは 0 語。4 桁の年（1100〜2099）は 3 語、
   その他の数は 3 桁の組ごとに読みを数え（`1,200` = 4 語）、小数は `point` + 1 桁 1 語、通貨記号・`%`・
   `K/M/B/T` は +1 語（`$5.2B` = 5 語）。過大に数える方向に倒す（超過は再生成になるだけで音声は溢れない）

### Consequences

- 良い: 台本の予算が描画の区間まで保たれ、区間の不足は有料の制作の前に storyboard の再生成で直る
- storyboard の再生成が増えうる（区間を縮める LLM 出力を拒否するため）。prompt に最短区間を渡して抑える
- **既知の負債（実装と文書の差）**: ADR-0014 / failure-policy の「同じ input_hash で規定ラウンド連続して同種の違反
  → `needs_input`（`PromptContractError`）」は台本・storyboard 工程で実装されていない。予算違反が続くと
  `max_attempts` を使い切り、`RETRY_BUDGET_EXHAUSTED` で Episode は `blocked` になる。昇格は実装しない
  （failure-policy の表は現実の挙動に合わせて書き直した）
- **負債**: 見積もりは規則ベースで、略語（`WWII`）・ローマ数字・記号の読みは 1 語として数える

