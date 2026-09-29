# ADR-0035: シーンの映像対象を Storyboard で決め、provider の内容拒否をシーン単位で復旧する

## Status

Accepted (2026-09-29)

## Context

2026-09-26/27 の日次 Episode `dedcf315` (sb4) と `ec44fadd` (sb2) は、fal の動画生成で
HTTP 422 `content_policy_violation` を受けて `blocked` になった。応答は
`loc: ["body","image_url"]`、`msg: "The images or videos provided may contain likenesses of real
people or other private information that cannot be processed."`、
`ctx.extra_info.reason: "partner_validation_failed"` だった。

調査で確認した事実（証拠）:

1. 拒否されたのは動画モデルへの**入力画像**（`body.image_url`）であり、テキストではない。
   画像モデル（Seedream）は同じ画像を生成できていた。fal 公式の errors ページは
   `content_policy_violation` を「入力（テキストや画像）が自動安全システムに検知された・再試行不可」
   と書くだけで、`partner_validation_failed` の意味と、拒否時の課金有無は書いていない。
2. 拒否された2枚はどちらも写実的な人物が画面の主役で、顔が識別できる大きさだった
   （`ec44fadd` sb2 は "Ieyasu" と名指し、`dedcf315` sb4 は名指しなしの僧兵数人）。同じ Episode で
   動画まで成功したシーンは地図・図解・建物・行列の遠景だった。過去に投稿済みの Episode では
   「無名の人物を背後から」も成功している。**どの特徴が判定の引き金かは、2件だけでは確定できない**
   （推測: 名指しの有無より「写実的で識別可能な顔が主題」であること）。
3. 名前・功績は既に台本の `narration` から字幕として伝わる（`domain/render/subtitles.py`）。
   映像から人物を外しても事実は失われない。
4. ADR-0034 は同じ `input_hash` の自動再送を止めたが、
   (a) 拒否の `loc`/`reason` は `error_summary` の文字列にしか残らず、
   (b) テキストだけを変えて**同じ画像**を再送する経路は塞いでいない（`video_input_hash` は
   テキストも含むので別 hash になる）。
5. **過剰な無効化**: `image_input_hash`/`video_input_hash`/`voice_input_hash` は storyboard **全体**の
   sha256 と、モジュール共有の prompt 組み立て規則の版（`style_profile_id`/`motion_profile_id`）を
   含む。1シーンを直すか版を上げると、全シーンの hash が変わる。再利用ゲート
   （`find_and_verify_current` / `find_latest_for_input`）は hash の完全一致でしか既存を見つけない
   ため、成功済みシーンまで再送・再課金される。連続試験
   `test_422_content_policy_rejection_blocks_same_input_retry_then_version_bump_mitigation`
   がこれを実証した。ADR-0034 で版を 1→2 に上げたため、そのままデプロイして本番の
   blocked Episode を再開すると、成功済みの全シーンが再課金される。
6. Episode 単位の自動生成回数・追加費用の上限は無い（`*_MAX_ROUNDS` は1回の workflow 実行内だけ）。

provider が 422 を返さないことは保証できない。したがって目標は
「拒否されにくい映像案を最初から選ぶ」「拒否されたらそのシーンだけを別の映像案に替えて完成させる、
それができなければ理由を示して止まる」「成功済みシーンを再生成・再課金しない」の3つとする。

## Decision

### (1) Storyboard の時点で各シーンの映像対象を決める

- platform 契約に `VisualSubject`（`site`/`map`/`document`/`artifact`/`building`/`landscape`/
  `crowd_distant`/`figure_anonymous`/`named_person`/`diagram`/`text_card`）を置き、
  `StoryboardScene.visual_subject`（任意、既定 `None`）を足す。旧 storyboard はそのまま読める。
- 外部の OpenMontage `scene_plan` スキーマは `additionalProperties: false` なのでキーを足さない。
  既存の任意項目 `required_assets[]` の `{"type": <VisualSubject>, "source": "generate"}` を
  1件だけ書かせ、adapter が `visual_subject` へ写す。欠落・語彙外は LLM 出力の形式不正として
  retryable（ADR-0014、既存の storyboard ラウンド）。
- storyboard prompt は実際の拒否理由（実在人物の肖像）を根拠に、「識別可能な実在人物の顔を
  画面の主題にしない。史跡・地図・文書・武具・建物・群衆の遠景で史実を示し、名前と功績は
  ナレーションで伝える」と指示する。**歴史上の人物を一律には禁じない**: `named_person` は
  選べるが、顔が主題にならない構図（小さく・遠景・背後）でのみ描く。
- 画像・動画プロンプトは `visual_subject` に応じた構図の指示を持つ。人物を含む対象では
  「人物は画面内で小さく、顔を識別可能な肖像として描かない」。これは描く対象そのものを変える
  指示であり、実在人物を肖像として描きながら名前だけ消す、といった判定のすり抜けはしない。

### (2) 拒否を構造化して保存し、同じ入力も同じ画像も自動で再送しない

- `ProviderRejectedError` に構造化した拒否（`types`/`locs`/`reason`/`message`/`http_status`）を
  持たせ、`JobFailed` を経由して新テーブル `provider_rejections` へ1件ずつ保存する
  （`rejected_input` = `image`/`prompt`/`unknown`、`source_media_sha256` = 動画の入力画像）。
- 再送の禁止（INV-32）: 同じ `input_hash`（ADR-0034）に加え、`rejected_input='image'` の拒否が
  ある入力画像 sha256 は、テキストが違っても同じ provider へ再送しない。
- 既存の本番データ: migration で `error_summary` が `ProviderRejectedError:` で始まる spent 予約に
  `input_rejected_by_provider=true` を立て、`provider_rejections` を文字列から一度だけ補完する
  （以後は構造化して保存するので文字列解析はこの migration だけ）。

### (3) 拒否されたシーンだけを別の映像案に替える（段階的な復旧）

`ProductionWorkflow`（`workflow.patched("scene-alternative-recovery-v1")`）:

1. あるシーンの画像または動画が構造化された拒否で止まったら、そのシーンについて
   `plan_scene_alternative` Activity を呼ぶ。port `SceneAlternativePlanner`
   （本番: Codex、試験: fake）に、元のシーン・そのシーンの台本 narration・拒否・過去の代替案を渡す。
2. 代替案は `SceneVisualOverride` Artifact（シーン単位、current は supersede で1本）として
   保存する。検証（domain）: 拒否が肖像なら `visual_subject` は人物を主題にしない集合
   （`named_person`/`figure_anonymous` を除く）、文面は元と過去の案のどれとも違う、
   planner が「事実を損なわない」根拠を書いている。planner は `infeasible`（理由つき）を返せる。
3. 代替案を適用した「実効シーン」でそのシーンの**画像から**作り直し、続けて動画を作る。
   他シーンは触らない。
4. 上限（`contracts/production_activities.py` に1箇所。INV-34）: 1シーンあたりの代替案回数、
   1 Episode あたりの代替案回数、1 Episode あたりの復旧による追加費用（拒否された spent 予約と、
   代替案で作り直した fal 予約の `estimated_cost_usd` の合計）。上限到達・`infeasible`・検証失敗は
   理由つきの `needs_input` で止まり、人間の判断を待つ。回数と費用は DB から数えるので、
   resume してもリセットされない。
5. 再開（ADR-0032）で旧 Episode を動かした場合も、再送禁止で止まったシーンは同じ復旧に入る。

### (4) シーン単位の同一性と「生成レシピの版」の分離（INV-33）

- 画像・動画の `input_hash` から storyboard 全体の sha256 を外し、**そのシーンの実効内容の指紋**
  （`scene_id`・`script_scene_id`・`duration_ms`・`visual_kind`・`visual_subject`・
  `visual_description`・`framing`・`camera_movement`・`transition_in` の正準 JSON）に置き換える。
  音声は台本と自分の担当シーンの時間割だけに依存させる。
- `artifact_metadata.content_fingerprint`（新列）に「レシピの版を除いた入力指紋」を保存する。
  再利用ゲートは (i) `input_hash` 完全一致、(ii) 同じシーンの現行 Artifact の
  `content_fingerprint` 一致（生成器は同じ・実体検証は通る・レシピの版だけ違う）、
  (iii) 旧方式（列が NULL）の Artifact は旧方式の hash を再計算して一致すれば再利用、の順に見る。
  レシピの版を上げても、既に成功したシーンは作り直さない（新しく作るシーンだけ新しい版を使う）。
- 未照合・submit 済みの予約は hash が変わっても (episode, provider, scene) で見つけて止める
  （hash 方式の変更で二重 submit を作らない）。

### (5) 測定

拒否率（provider 別）、代替案の回数、再試行回数、1本あたり費用、完成率を
`provider_rejections`/`provider_reservations`/`artifact_metadata`/`episodes` から読み取り専用で
集計する `scripts/production-metrics.py` を置く。

### (6) planner の worker

planner は Codex CLI を使うので、Codex を持たない production worker には載せない。workers は互いを
import しない（INV-3）ので storyboard worker にも相乗りさせず、compose service
`scene-alternative-worker`（queue `production-scene-alternative`）を足す。Codex の sandbox のため
storyboard-worker と同じ seccomp / `SETFCAP` の例外を持つ（ADR-0024 の Codex worker が3つになる）。
worker が止まっていれば計画 Activity は schedule_to_close で timeout し、そのシーンは `needs_input`
で止まる（自動では進まない側）。Activity 名・入出力・queue は `contracts/production_activities.py`。

既知の重複（今回は直さない・AGENTS.md §3）: Codex の既定モデルの来歴ラベル
`"codex-config-default"` が `workers/planning/activities.py` と `workers/storyboard/activities.py` に
literal で重複している。新しい worker は `contracts.production_activities.CODEX_DEFAULT_MODEL_LABEL`
を参照する。既存2箇所の寄せは別タスク。

### (7) 追補: 「入力を取得できなかった」は内容の拒否ではない（2026-09-29 の実例）

2026-09-29 06:00 JST の日次 Episode `54392404` は sb1〜sb4 の動画が成功した後、sb5 の動画で
HTTP 422 `file_download_error`（`loc: body.image_url`、"Failed to download the file"）を受けて
`blocked` になった。fal 公式の errors ページは「入力の URL のファイルを取得できなかった」
（retryable: false、URL が公開で取得可能か確認せよ）と書く。**入力の内容の判定ではない**。

当時のコードは 422 を一律に `ProviderRejectedError`（内容の拒否）にしていた。そのままだと
本 ADR の仕組みが、無関係な画像を再送禁止にし（INV-32）、`input_rejected_by_provider` で resume でも
取り直せなくし、別の映像案の計画まで走らせる。そこで第3の結果として分ける:

- fal adapter: `file_download_error` → `ProviderInputFetchError`（needs_input。`ProviderRejectedError`
  の派生にしない）→ `JobFailed(input_unreachable=True)`
- `PaidJobRunner`: 予約は spent（ジョブは終わっている）、`input_rejected_by_provider` は立てない、
  拒否台帳にも載せない。同じ実行の中では再送しない（fal: retryable=false）
- 復旧（代替案の計画）は起動しない（`ProviderRejectedError` 系だけが対象）
- 人が resume すれば同じ入力でも新しいラウンドとして1回取り直す（画像は prepare で上げ直すので
  別の URL になる）
- migration 0014 の補完は `file_download_error` だけの行を補完しない（本番の3行のうち2行だけ）

自動で1回取り直すか（fal は retryable=false と書くが、次の依頼は別の URL になる）は所有者の判断に
残す（既定は止まる側）。

## Alternatives

- **(a) 版を上げて全シーンを作り直す（ADR-0034 の状態）** — 実装は無いが、1シーンの拒否で
  全シーンを再課金する。連続試験で実害を確認済み。却下。
- **(b) 人物名をプロンプトから消すだけ** — 同じ写実的な人物画像を作り続け、拒否理由に
  対処しない。判定のすり抜けに近い。却下。
- **(c) 拒否された画像に動画用テキストだけ変えて再送** — `loc` は `image_url`。同じ画像なら同じ
  判定になるはずで、費用だけが増える。却下（INV-32 で禁止）。
- **(d) 代替案を新しい storyboard 世代として保存** — storyboard 全体の世代が変わり、他シーンの
  同一性まで巻き込む。シーン単位の override にする。
- **(e) 代替案を決定的なテンプレートで作る** — 文脈（何の史実を示すシーンか）を失い、
  事実関係を損なう。LLM に計画させ、結果は決定的な規則で検証する。
- **(f) 歴史上の人物を一律に禁止** — 根拠（実際の拒否理由・provider 規則）を超える制約で、
  背後・遠景の人物が通っている実績とも合わない。却下。

## Consequences

**良い側**
- 1シーンの拒否でそのシーンと、それに依存する manifest・render・upload だけが作り直される。
- 同じ入力・同じ画像への無駄な再送が起きない。自動の試行と費用に上限がある。
- 拒否の理由がシーン単位で構造化されて残り、拒否率・費用・完成率を測れる。

**悪い側 / 引き受けた負債**
- 拒否が起きないことは保証できない。provider の判定器は非公開で、`partner_validation_failed`
  の意味も未文書。構図の指示は根拠のある緩和策であって確証ではない。
- 代替案が史実を損なわないかは LLM の自己申告と規則検査でしか見ていない。人間の確認は
  `SceneVisualOverride` の `rationale` を読むことに依存する。
- 拒否時の課金有無は不明なので、拒否された試行も費用として数える（保守側）。
- identity の二方式（旧・新）が共存する。旧方式の再計算コードは、旧 Artifact を持つ Episode が
  無くなるまで残す。
- 代替案の planner は Codex の追加呼び出しを増やす（サブスクリプション実行で per-call 課金は無い）。

## 陳腐化条件

- fal が `partner_validation_failed` の判定基準や、拒否時の課金を文書化したとき
- provider を変えたとき（肖像判定の有無・基準が変わる）
- 拒否率の実測（`scripts/production-metrics.py`）が上限値の見直しを要する水準になったとき

## 語彙・設定キーの追加（AGENTS.md §8 の grep）

`grep -rn "<key>" apps/ workers/ domain/ infrastructure/ contracts/ docs/` を変更前
（`8a8c499`）に実行した結果、次のキーのヒットはすべて 0 件（読み手・書き手とも既存なし）。
`rejected_input` の 5 件は既存テスト名 `test_provider_rejected_input_*` の部分一致で無関係。

| キー | 定義元（1箇所） |
|---|---|
| `plan_scene_alternative` / `scene_visual_override` / `codex_scene_alternative` | `contracts/states.py` |
| `RejectedInput`（`image`/`prompt`/`unknown`） | `contracts/states.py` |
| `VisualSubject` / `StoryboardScene.visual_subject` / `SceneVisualOverrideArtifact` | `contracts/artifacts.py` |
| `MAX_SCENE_ALTERNATIVES_PER_SCENE` / `_PER_EPISODE` / `MAX_RECOVERY_COST_USD_PER_EPISODE` | `contracts/production_activities.py` |
| `artifact_metadata.content_fingerprint` / `provider_rejections` | `infrastructure/db/models.py`（migration 0014） |

## 機械検査

INV-32〜34 は `docs/invariants.md`（各項の機械検査欄にテスト名）。連続試験（fake provider・隔離環境）:

- `tests/integration/test_scene_rejection_recovery_e2e.py::test_only_the_rejected_scene_is_replanned_and_regenerated_through_private_upload`
  — 通常生成 → 1シーンだけ 422（await・`body.image_url`）→ 代替案 → そのシーンだけ画像から
  再生成 → Render → private Upload。成功済みシーンの submit 回数・日次枠・投稿回数が増えないこと
- `tests/integration/test_incident_recovery_e2e.py::test_422_rejection_then_recipe_version_bump_rebills_nothing_and_never_resends_the_image`
  — 版上げだけでは何も再課金せず、拒否された画像も送らず、planner 不在なら止まること

テストごとの設計根拠: `docs/testing/scene-rejection-recovery-rationale.md` /
`scene-identity-rationale.md` / `storyboard-visual-subject-rationale.md`。
