# Artifact

## 定義

**Artifact = 工程が生み出した成果物**。台本JSON、素材マニフェスト、
動画ファイル、投稿レシート、実績レポートなど。

Artifactは v2 の**再開の単位**である。工程ごとの再開フラグを作らないのは、
「出力Artifactが既にあるか」という1つの述語に集約するため
（[failure-policy §4](../failure-policy.md)）。

## 保存先

- **本体**: MinIO（INV-9）。キーは content-addressed
  （`artifacts/{episode_id}/{artifact_type}/{sha256}.json`）。
  キーの形は `domain/artifact/keys.py` の `artifact_object_key()` が唯一の定義。
  シーン単位の Artifact は `artifacts/{episode_id}/{artifact_type}/{scene_id}/{sha256}.json`（ADR-0018）
- **メディア本体**（画像・音声・動画のバイナリ）: `media/{episode_id}/{artifact_type}/{scene_id}/{sha256}.{ext}`
  （`media_object_key()`、ADR-0017）。Artifact JSON の `media` 記述子から参照する
- **参照とメタ**: PostgreSQL

DBにバイナリを入れない。書き順は必ず MinIO → DB。

## 属性

**Phase 1 で実装済み**（`infrastructure/db/models.py` の `ArtifactMetadataRow`）:

- `id`, `episode_id`, `artifact_type`, `schema_version`
- `bucket`, `object_key`, `sha256`, `size_bytes`
- `produced_by_job_id`
- `created_at`

**Phase 2 で実装済み**（ADR-0012）:

- `input_hash` — この成果物を作った**入力**の指紋。非決定的な生成器（LLM）では
  sha256 が毎回変わるため、**同一性の軸はこちら**
- `version` — 同じ `(episode_id, artifact_type)` 内で単調増加
- `superseded_at` — NULL なら現行世代。partial unique index により
  「現行は常に1本」を DB が保証する

**Phase 4 で実装済み**（ADR-0018）:

- `scene_id` — シーン単位の Artifact（`scene_image` / `scene_voice` / `scene_video`）。
  `version` / `superseded_at` / 同一内容の判定はすべて `(episode_id, artifact_type, scene_id)` の中で閉じる。
  NULL は Episode 単位（script / storyboard / production_manifest）

**まだ無いもの**:

- `content_type`

## immutability

一度書いたオブジェクトは上書きしない（INV-11）。作り直しは
新しい `version` を作り、古い方を `superseded` にする。

理由: 再実行のたびに上書きしていると、「どのバージョンの素材から
どの動画が作られたか」が失われ、失敗の再現ができなくなる。
前身repoでこれが起き、レンダー失敗の原因追跡が不可能になった。

## schema

- すべてのArtifactはスキーマを持つ（INV-10）。Phase 1 の置き場は
  `contracts/artifacts.py`（`contracts/schemas/` への分割は Phase 2）
- 読み込み時に必ず検証する。想定外の `schema_version` は
  `permanent` 失敗（推測して読まない）
- 後方互換を壊すスキーマ変更はADRを要する（AGENTS.md §6）

## 想定するschema（Phase 1で定義予定）

| artifact_type | 生成worker | 内容 |
|---|---|---|
| `episode_plan` | planning | 企画（トピック、切り口、想定尺） |
| `script` | planning（Phase 2 実装済み） | 台本（title / hook / scenes / metadata） |
| `storyboard` | storyboard（Phase 3 実装済み、ADR-0015） | シーン分割と各シーンの映像指示・尺（旧予定名 `scene_plan`）。ナレーションは持たず `script_scene_id` で台本を参照 |
| `scene_image` | production image（Phase 4、ADR-0017） | storyboard シーンの静止画（1080x1920）。メディア本体への記述子 |
| `scene_voice` | production voice（Phase 4、ADR-0017） | 台本シーンのナレーション音声。ナレーション文は持たず台本を参照 |
| `scene_video` | production video（Phase 4、ADR-0017） | storyboard シーンの動画（元画像を参照、音声なし、fps は `fps_millis`） |
| `production_manifest` | production（Phase 4、ADR-0017。旧予定名 `asset_manifest`） | 全シーンの画像・動画・音声の参照一覧 |
| `edit_decisions` | render | 編集判断（字幕、オーバーレイ、BGM） |
| `final_video` | render | 完成動画 |
| `review_report` | render | 品質ゲートの判定結果 |
| `video_metadata` | upload | タイトル・説明・タグ |
| `upload_receipt` | upload | YouTube video_id と投稿時刻 |
| `performance_report` | analytics | 実績メトリクス |

## 再開判定（Phase 2 以降）

非決定的な生成器（LLM）は同じ入力でも違う内容を返すので、
**「同じ内容なら同じキー」では「同じ入力から作られたか」を判定できない**。
したがって再開判定は `input_hash` で行う（ADR-0012）。

- **生成器を呼ばない条件（skip）**: `superseded_at IS NULL` かつ `input_hash` 一致の
  Artifact が既にある → それを返し、Job を `skipped` にする
- **呼ぶ条件**: `input_hash` が変わった / 前ラウンドが検証で落ちた /
  人間が明示的に再生成を要求した。**この3つ以外で有料呼び出しをしない**

## Artifactが持たないもの

- 状態遷移の判断
- 「次にどの工程が必要か」の情報
