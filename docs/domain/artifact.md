# Artifact

## 定義

**Artifact = 工程が生み出した成果物**。台本JSON、素材マニフェスト、
動画ファイル、投稿レシート、実績レポートなど。

Artifactは v2 の**再開の単位**である。工程ごとの再開フラグを作らないのは、
「出力Artifactが既にあるか」という1つの述語に集約するため
（[failure-policy §4](../failure-policy.md)）。

## 保存先

- **本体**: MinIO（INV-9）。キーは content-addressed
  （`artifacts/{episode_id}/{schema_name}/{content_sha256}`）
- **参照とメタ**: PostgreSQL

DBにバイナリを入れない。書き順は必ず MinIO → DB。

## 属性

- `id`, `episode_id`, `schema_name`, `schema_version`
- `version` — 同じ `(episode_id, schema_name)` 内で単調増加（INV-10）
- `bucket`, `object_key`, `content_sha256`, `size_bytes`, `content_type`
- `produced_by_job_id`
- `input_hash` — この成果物を作った入力の指紋
- `status`: `current` / `superseded`
- `created_at`

## immutability

一度書いたオブジェクトは上書きしない（INV-11）。作り直しは
新しい `version` を作り、古い方を `superseded` にする。

理由: 再実行のたびに上書きしていると、「どのバージョンの素材から
どの動画が作られたか」が失われ、失敗の再現ができなくなる。
前身repoでこれが起き、レンダー失敗の原因追跡が不可能になった。

## schema

- すべてのArtifactは `contracts/schemas/` にスキーマを持つ（INV-10）
- 読み込み時に必ず検証する。想定外の `schema_version` は
  `permanent` 失敗（推測して読まない）
- 後方互換を壊すスキーマ変更はADRを要する（AGENTS.md §6）

## 想定するschema（Phase 1で定義予定）

| schema_name | 生成worker | 内容 |
|---|---|---|
| `episode_plan` | planning | 企画（トピック、切り口、想定尺） |
| `script` | planning | 台本と出典 |
| `scene_plan` | planning | シーン分割と各シーンの指示 |
| `asset_manifest` | generation | 生成素材の一覧と参照 |
| `edit_decisions` | render | 編集判断（字幕、オーバーレイ、BGM） |
| `final_video` | render | 完成動画 |
| `review_report` | render | 品質ゲートの判定結果 |
| `video_metadata` | upload | タイトル・説明・タグ |
| `upload_receipt` | upload | YouTube video_id と投稿時刻 |
| `performance_report` | analytics | 実績メトリクス |

## Artifactが持たないもの

- 状態遷移の判断
- 「次にどの工程が必要か」の情報
