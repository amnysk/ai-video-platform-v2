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
