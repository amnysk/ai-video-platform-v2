# データフロー

## 1. 正常系（企画から分析まで）

```text
[UI] ──POST /episodes──> [FastAPI]
                            │ 1. 入力検証
                            │ 2. INSERT episode (state=planned) ── PostgreSQL
                            │ 3. start_workflow(EpisodeWorkflow, episode_id)
                            └──> 202 Accepted {episode_id}

[Temporal] EpisodeWorkflow
    │
    ├─ Activity: plan          → Artifact(episode_plan v1)      → MinIO + DB参照
    ├─ Activity: write_script  → Artifact(script v1)
    ├─ Activity: plan_scenes   → Artifact(scene_plan v1)
    ├─ Activity: produce_*     → Artifact(scene_* / production_manifest v1) + 素材   [有料]
    ├─ Activity: render        → Artifact(final_video v1)   入力は production_manifest（ADR-0019）
    ├─ Activity: quality_gate  → Artifact(review_report v1)
    │      └─ 不合格 → 失敗クラス分類 → retryable なら再生成へ分岐
    ├─ (signal待ち: 人間承認が要る場合)
    ├─ Activity: package       → Artifact(video_metadata v1)
    ├─ Activity: upload        → Artifact(upload_receipt v1)     [private投稿]
    └─ (timer: 成熟待ち)
       Activity: fetch_metrics → Artifact(performance_report v1)
```

各Activityの前後で、domainの遷移規則を通してEpisode/Job状態がPostgreSQLへ書かれる。

## 1b. storyboard 工程（Phase 3 実装済み / ADR-0015）

```text
[UI] ──POST /episodes/{id}/storyboard──> [FastAPI]
        │ Episode の存在確認（404）→ start_workflow(StoryboardWorkflow, id=episode-{id}-storyboard)
        └──> 202（状態の前提は判定しない。workflow の admit が判定する）

[Temporal] StoryboardWorkflow（task queue "storyboard"、host process の worker）
    ├─ storyboard_admit_episode   script_ready|storyboard_ready → in_progress（他は何もせず終了）
    ├─ storyboard_create_job      非終端の plan_storyboard job を再利用、無ければ作成
    ├─ storyboard_generate × ラウンド（maximum_attempts=1、retry は workflow のラウンド）
    │     現行 script を読む（sha256 + 契約検証）→ input_hash
    │     → 同じ input_hash の現行 storyboard があれば job=skipped で返す（生成器を呼ばない）
    │     → 予約 commit → dispatch commit → 生成 → 生出力 put → spent commit
    │     → 解釈 → 時間軸正規化 → 採番 → 契約 → 台本カバレッジ
    │     → MinIO put → 読み戻し sha256 照合 → artifact_metadata 記録 → job succeeded
    └─ storyboard_mark_ready（in_progress → storyboard_ready）| storyboard_record_failure
```

## 1c. render 工程（Phase 5 / ADR-0019）

```text
[UI] ──POST /episodes/{id}/render {render_profile_id?}──> [FastAPI]
        │ profile id の検証（4xx）→ start_workflow(RenderWorkflow, id=episode-{id}-render)
        └──> 202（重複は 409）

[Temporal] RenderWorkflow（task queue "render"）
    ├─ render_admit          assets_ready|render_ready → in_progress（render 自身の needs_work|blocked は再開）
    ├─ render_final_video    （単一の重い Activity、heartbeat ≤10秒）
    │     現行 production_manifest を PG から解決 → script / storyboard / 音声 / 動画を MinIO から読み
    │     sha256 照合 + 契約検証 → 描画計画 → input_hash
    │     → 同じ input_hash の現行 final_video があれば job=skipped で返す
    │     → 空き容量の事前検査 → 作業領域 → 描画 → 実測 → 技術検査（合格のみ先へ）
    │     → mp4 と JSON を MinIO put → 読み戻し sha256 照合 → artifact_metadata 記録 → job succeeded
    └─ render_mark_ready（in_progress → render_ready）| render_record_failure
```

## 2. Artifactの読み書き

```text
Activity開始
   │
   ├─ 入力: DBからArtifact参照を引く → MinIOから本体を取得 → schemaで検証
   │        （schema_version が想定外なら permanent 失敗）
   │
   ├─ 出力が既に存在し input_hash が一致 → skipして既存参照を返す（再開）
   │
   └─ 出力: MinIOへ put（キーは content-addressed）
            → DBへ artifact 行を INSERT（version = 既存+1）
            → 同一トランザクションで Job を succeeded に
```

**書き順は必ず MinIO → DB。** DB行があるのに本体が無い状態を作らない。
逆（MinIOに孤児が残る）は許容し、GCで回収する。

## 3. 失敗時

```text
Activity が例外送出
   │
   ├─ TransientError    → Temporal RetryPolicy で自動retry。DBは触らない
   ├─ RetryableError    → Job=failed(retryable), Episode=needs_work
   │                       workflowが再生成分岐へ。上限超過なら blocked
   ├─ NeedsInputError   → Job=failed(needs_input), Episode=blocked
   │                       workflowはsignal待ちで**生きたまま**停止
   └─ PermanentError    → Job=failed(permanent), Episode=failed (terminal)

分類不能の例外 → NeedsInputError として扱う（INV-12）
```

他のEpisodeのworkflowには一切影響しない（INV-13）。

## 4. 再実行（UIから）

```text
[UI] ──POST /episodes/{id}/retry {from_stage}──> [FastAPI]
        │ 1. Episodeがretry可能な状態か domain規則で検証
        │ 2. from_stage 以降の Artifact を superseded に印付け
        │ 3. workflowへ signal（実行中）または新workflow start（終了済み）
        └──> 202

未印付けのArtifactは input_hash 一致でskipされるので、
from_stage より前の工程は課金なしで素通りする。
```

## 5. 投稿の冪等性

```text
upload Activity
   │
   ├─ final_video を検証（PG メタデータ → MinIO JSON sha256 → 契約 → メディアの sha256）
   ├─ upload key = sha256(stage, episode_id, final_video sha256, チャンネル)   （ADR-0020 §3）
   ├─ provider_reservations に予約（UNIQUE(idempotency_key)、youtube_upload、1ラウンドのみ）
   │     └─ SPENT + video id あり → YouTube を呼ばず receipt を返して終了
   ├─ session 開始 → session URI を予約に保存 → dispatched_at を commit
   ├─ チャンク送信（失敗は status query から再開）
   │     └─ session 失効 / 結果不明 → マーカー照合。無ければ blocked（**新しい session を開かない**）
   ├─ video id を予約に保存（SPENT）
   └─ upload_receipt を保存 → Episode uploaded
```

INV-14 / INV-15。プロセスが外部呼び出しの最中に死んでも、
再開時は照会から入り、二重投稿・二重課金を作らない。

## 6. 観測

すべてのworkflow/activityにtrace contextを伝播させ、
`episode_id` / `job_id` / `stage` / `failure_class` を属性として持たせる。
Prometheusには少なくとも:
`episodes_by_state`, `jobs_failed_total{failure_class}`,
`provider_cost_jpy_total`, `stalled_episodes`。
secretは属性に載せない（INV-20）。
