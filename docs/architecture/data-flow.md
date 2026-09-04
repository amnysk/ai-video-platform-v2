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
    ├─ Activity: generate      → Artifact(asset_manifest v1) + 素材   [有料]
    ├─ Activity: render        → Artifact(final_video v1)
    ├─ Activity: quality_gate  → Artifact(review_report v1)
    │      └─ 不合格 → 失敗クラス分類 → retryable なら再生成へ分岐
    ├─ (signal待ち: 人間承認が要る場合)
    ├─ Activity: package       → Artifact(video_metadata v1)
    ├─ Activity: upload        → Artifact(upload_receipt v1)     [private投稿]
    └─ (timer: 成熟待ち)
       Activity: fetch_metrics → Artifact(performance_report v1)
```

各Activityの前後で、domainの遷移規則を通してEpisode/Job状態がPostgreSQLへ書かれる。

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
   ├─ 冪等キー = (episode_id, final_video.version)
   ├─ DBに upload_attempt を INSERT（キーにUNIQUE制約）
   │     └─ 既存行があり status=succeeded → その receipt を返して終了
   │     └─ 既存行があり status=in_flight → YouTube側を照会して照合
   │                                        （**再送しない**）
   ├─ YouTube resumable upload 実行
   └─ upload_attempt を succeeded + video_id で更新
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
