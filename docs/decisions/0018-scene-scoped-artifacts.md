# ADR-0018: シーン単位の Artifact・job・予約

## Status

Accepted (2026-09-13)

## Context

ADR-0012 は Artifact の同一性を `(episode_id, artifact_type)` の中の `input_hash` / `version` /
`superseded_at` で管理し、「現行は常に1本」を partial unique index で保証した。
script と storyboard は Episode に1本なのでこれで足りた。

Phase 4（ADR-0017）の `scene_image` / `scene_voice` / `scene_video` は **1 Episode に同じ型が
シーン数だけ並ぶ**。現行の制約のままでは sb2 の画像を記録した瞬間に sb1 の画像が superseded になり、
「現行は1本」が「シーンが1つしか持てない」に化ける。同様に:

- `jobs`: 非終端 job の再利用を `(episode, type)` で引くと、シーン間で job を取り違える
- `provider_reservations`: 未照合予約の検査が Episode + provider 単位なので、
  sb1 の曖昧な予約が並行する sb2〜sbN を全部止める

## Decision

**`artifact_metadata` / `jobs` / `provider_reservations` に nullable の `scene_id` を足し、
一意性と現行の判定を scene キー `coalesce(scene_id, '')` の中で閉じる。**

1. `artifact_metadata.scene_id`（`String(16)` NULL）。一意性の索引を張り替える（migration 0004）:
   - `uq_artifact_metadata_content`: `(episode_id, artifact_type, coalesce(scene_id,''), sha256)`
   - `uq_artifact_metadata_version`: `(episode_id, artifact_type, coalesce(scene_id,''), version)`
   - `uq_artifact_metadata_current`: `(episode_id, artifact_type, coalesce(scene_id,''))` WHERE `superseded_at IS NULL`
2. リポジトリ: `record(..., scene_id=None)` / `find_current_by_type(episode_id, type, scene_id=None)` /
   `find_current(episode_id, type, input_hash, scene_id=None)`。`version` の採番と supersede は
   **同じ scene キーの中だけ**。`scene_id=None` は「Episode 単位の行（`scene_id IS NULL`）」を意味し、
   **script / storyboard は Phase 3 までと完全に同じ挙動**になる（回帰の要）。
   マニフェスト用に全シーンの現行を引く `list_current_by_type` を足す
3. `jobs.scene_id`: `JobRepository.create(..., scene_id=None)` と `find_open(episode_id, type, scene_id=None)`
4. `provider_reservations.scene_id`: `find_unreconciled(episode_id, provider, scene_id=None)` は
   scene キーで範囲を閉じる。並行するシーンが互いを止めない
5. `provider_reservations.provider_job_ref TEXT NULL`（ADR-0017 §3）と
   `estimated_cost_usd NUMERIC(10,4) NULL`。後者で ADR-0013 Alternatives (e)（コスト列を入れない）を解決する:
   fal は per-call 課金で、予約時点の見積もりに読み手（運用の支出確認）ができたため。値は見積もりであり確定額ではない
6. `ProviderReservationRepository.record_provider_job_ref(reservation_id, ref)`:
   `reserved` かつ `dispatched_at` ありで参照が NULL のときだけ書ける。同じ参照は no-op、
   異なる参照は `InvalidTransitionError`（二重 submit の兆候なので上書きしない）
7. オブジェクトキー: Artifact 記述 JSON は `artifacts/{episode}/{type}/{scene}/{sha256}.json`、
   メディア本体は `media/{episode}/{type}/{scene}/{sha256}.{ext}`（`domain/artifact/keys.py`）。
   `scene_id` を省略した `artifact_object_key` は従来のキーのまま

## Alternatives

**(a) シーンごとに ArtifactType を分ける（`scene_image_sb1` …）** — スキーマ変更が不要。
しかし語彙がシーン数に比例して増え、CHECK 制約が破綻する。却下。

**(b) シーン素材を1つの Artifact（全シーンの配列）にまとめる** — 現行制約のままで済む。
しかし1シーンの再生成で全体が新世代になり、シーン単位の再開（INV-17）が効かない。却下。

**(c) `scene_id` を NOT NULL（Episode 単位は `''`）にする** — 式索引が要らない。
しかし既存行の書き換えが必要で、「シーンを持たない」と「空のシーン名」が区別できなくなる。却下。

**(d) 別テーブル `scene_artifacts` を作る** — 既存テーブルに触れない。しかし再開判定・世代管理・
予約の紐付け（`outcome_artifact_id`）を2系統持つことになる。却下。

## Consequences

**良い側**
- シーンごとに独立した世代・現行・再利用・job・未照合予約の検査が DB 制約で保証される
- Episode 単位の Artifact（script / storyboard）の意味は変わらない

**悪い側 / 引き受けた負債**
- 一意性が**式索引**になった。SQLAlchemy / Alembic の reflection は式索引を読めず、
  SQLite の `batch_alter_table`（テーブル再作成）は式索引を黙って失う。今後 `artifact_metadata` を
  batch で変更する migration は、式索引を明示的に作り直すこと
  （検査: `tests/contract/test_migration_matches_models.py::test_migration_creates_the_modelled_indexes`）
- `scene_id=None` の既定は「Episode 単位」を意味するので、シーン素材の呼び出しで渡し忘れると
  **何も見つからない**（安全側だが再生成が走る）。worker 側のテストで固定する
- `scene_id` の語彙（`sb*` / `s*`）は DB では検査しない（契約とキー規約で検査する）

## 陳腐化条件

- シーン以外の粒度（例: カット、字幕行）の Artifact が必要になったとき
- `artifact_metadata` の一意性を再度変更するとき（式索引の作り直しを忘れない）
