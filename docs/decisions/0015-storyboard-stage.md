# ADR-0015: Storyboard 工程と Episode `storyboard_ready`

## Status

Accepted (2026-09-13)

## Context

Phase 3 は台本（`script`）から絵コンテ（storyboard: シーン分割・各シーンの映像指示・尺）を作る。
Phase 3 の workflow は **storyboard の生成・検証で終わる**（画像 / 音声 / 動画 / 投稿は Phase 4 以降）。

ADR-0011 は「工程ごとに状態を足さない。**駐機が必要な箇所（＝そこで workflow が終わる箇所）だけ**
が状態を持つ」と定め、「Phase 3 で `storyboard_ready` を足したくなったとき、本当に駐機するのかを
問い直すこと」を負債として残した。本ADRはその問い直しの記録である。

問い直しの結果: Phase 3 の `StoryboardWorkflow` は storyboard を作った時点で終わり、次工程は
まだ存在しない。つまり Episode は storyboard の後で**実際に駐機する**。`in_progress` のまま置けば
ADR-0011 が退けたのと同じ「出口の無い `in_progress`」になり、`script_ready` のまま置けば
「storyboard まで出来たか」を状態が嘘をつく（`script_ready → STAGE_ADMITTED` で既に出ている）。

語彙の追加箇所を AGENTS.md §8 の手順で洗った（`grep -rn "script_ready\|SCRIPT_READY\|WRITE_SCRIPT\|CODEX_SCRIPT"
apps/ workers/ domain/ infrastructure/ contracts/ docs/`、ced2aae 時点）。
書き手は `contracts/states.py`（定義）と migration（CHECK の貼り替え）、読み手は
`domain/episode/transitions.py`・`infrastructure/db/models.py`（CHECK を enum から導出）・
`workers/planning`・docs 表。どれも enum から導出しており literal の再掲は migration の
downgrade 語彙だけ（0002 と同じ構造）。

## Decision

**Storyboard を platform 所有の Artifact 契約として追加し、Episode に駐機点 `storyboard_ready` を足す。**

1. 語彙（`contracts/states.py`）
   - `EpisodeStatus.STORYBOARD_READY = "storyboard_ready"`（非terminal）
   - `JobType.PLAN_STORYBOARD = "plan_storyboard"`（job と artifact を同名にしない規約に従う）
   - `ArtifactType.STORYBOARD = "storyboard"`（artifact.md で予定していた `scene_plan` 行を実現する）
   - `ProviderCall.CODEX_STORYBOARD = "codex_storyboard"`。Codex は有料の LLM 呼び出しなので
     INV-15 の予約台帳に載せる。台本と値を分けるのは、未照合予約の検査を工程ごとに独立させるため
   - `STORYBOARD_WORKFLOW = ("StoryboardWorkflow", "storyboard")`（workflow 名, task queue）
2. 遷移（2行 + 事象 `EpisodeEvent.STORYBOARD_READY`）
   - `in_progress + STORYBOARD_READY` → `storyboard_ready`
   - `storyboard_ready + STAGE_ADMITTED` → `in_progress`（Phase 4 の入口）
3. **`Pipeline.STORYBOARD` は足さない。** `Pipeline` は Episode **作成時**に起動する workflow の語彙であり、
   storyboard は既存 Episode（`script_ready`）に対して起動する。作成時の選択肢に混ぜると
   「台本の無い Episode に storyboard を走らせる」経路を API が開いてしまう
4. **Storyboard 契約は OpenMontage の `scene_plan` スキーマと別物**（`contracts/artifacts.py::StoryboardArtifact`）。
   外部スキーマは生成器の実装詳細であり（ADR-0016）、差し替え・版上げで我々の保存形式が揺れてはならない。
   - 時間は int ミリ秒（秒 float を持たない。ScriptArtifact と同じ理由）
   - `scene_id`（`sb1..sbN`）・`order`・`start_ms` は**システムが採番**する。LLM に決めさせない
   - ナレーションは複製しない。`script_scene_id` で台本を参照する（単一の真実）
   - `source_script`（artifact_id / sha256 / schema_version）で入力台本を固定する
   - `total_duration_ms` は保存し、台本の総尺と一致しなければならない（ドメイン検査
     `domain/storyboard/coverage.py::check_storyboard_covers_script`）
5. `input_hash`（ADR-0012）の構成要素は `domain/storyboard/identity.py::storyboard_input_hash` に1つだけ置く:
   episode_id / artifact_type / 目標 schema_version / **入力台本の sha256** / プロンプトテンプレートIDと版 /
   generator_id / generation_spec_id。ラウンド・試行・job_id・時刻・run id は含めない。
   冪等キーは `domain/script/identity.py::idempotency_key` を再利用する（複製しない）
6. 失敗クラス（`domain/errors.py`、ADR-0014 に従う）
   - `StoryboardOutputUnparseableError` / `StoryboardSchemaViolationError`: `retryable`
   - `StoryboardInputMissingError`（現行台本が無い）/ `StoryboardInputInvalidError`
     （保存台本が読めない・sha 不一致）/ `GenerationSpecUnavailableError`（固定した仕様が読めない）: `needs_input`
   - `WorkspaceUnavailableError`（一時作業領域を安全に用意できない）: `retryable`

入力台本が無いことを `permanent` にしないのは、Phase 2 の台本 workflow を人間が再実行すれば
回復するから（failure-policy §1 の `permanent` は「同じ入力で必ず同じ失敗」かつ回復経路が無い場合）。

## Alternatives

**(a) 状態を足さず `script_ready` に戻す / `in_progress` に置く** — 語彙が増えない。
しかし前者は「storyboard まで出来た」を表せず次工程が台本からやり直すか artifact を毎回探す必要があり、
後者は ADR-0011 が退けた出口の無い駐機になる。却下。

**(b) OpenMontage の `scene_plan` JSON をそのまま Artifact として保存する** — 変換が要らない。
しかし AGPL の第三者スキーマが我々の永続化契約になり、upstream の変更（実際に共有 checkout には
`generation_duration_seconds` を必須にする未コミット変更がある）で過去の Artifact が読めなくなる。
秒 float で正準JSONが揺れる問題も持ち込む。却下。

**(c) `Pipeline.STORYBOARD` を足して Episode 作成時にも起動できるようにする** — API が一様になる。
上記 3 の理由で却下。既存テスト `test_unknown_pipeline_is_rejected` の意味も保つ。

**(d) 台本と同じ `ProviderCall.CODEX_SCRIPT` を使う** — 語彙が増えない。しかし未照合予約の検査
（`UnreconciledReservationError`）が工程をまたいで干渉し、台本の未照合で storyboard が止まる。却下。

**(e) storyboard にナレーションを複製する** — Phase 4 が台本を読まずに済む。同じ真実が2箇所に散るので却下。

## Consequences

**良い側**
- Phase 3 の workflow が出口のある状態で終端する
- 保存形式が platform 所有で、生成器（OpenMontage 誘導の Codex）を差し替えても Artifact は揺れない
- 入力台本の sha256 が `input_hash` に入るので、台本が再生成されれば storyboard も必ず再生成対象になる

**悪い側 / 引き受けた負債**
- **ADR-0011 の「前例」の懸念が現実化した。** 工程ごとに `*_ready` が増える形になった。
  本ADRは「workflow がそこで終わるから」だけを根拠にしており、Phase 4 で workflow が storyboard の先へ
  続けて進むなら `script_ready` / `storyboard_ready` の両方を自己ループへ畳む再評価が要る
- `generation_spec_id` を `input_hash` に入れたので、固定する OpenMontage commit を上げると
  全 Episode の storyboard が再生成対象になる（意図した挙動だが課金を伴う）
- `storyboard_ready` の出口 `STAGE_ADMITTED` は Phase 4 まで呼び出し元が無い（ADR-0011 と同じ期間の空白）
- 台本の「シーン数 3..8」と storyboard の「1..24」は独立に決めた値で、両者の整合を保証する機械は
  カバレッジ検査（全台本シーンが1回以上現れる）だけ

## 陳腐化条件

- Phase 4 の workflow が storyboard の先へ続けて進むようになったとき → `storyboard_ready` の駐機点としての必要性を再評価
- `input_hash` の構成要素を変えるとき（過去 Artifact と一致しなくなり全 Episode が再生成される）
- OpenMontage 以外の生成器を導入し、`generation_spec_id` の意味が一般化できなくなったとき
