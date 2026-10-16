# ADR-0020: Upload 工程（YouTube private 投稿）と Episode `uploaded`

## Status

Accepted (2026-09-15)

## Context

Phase 5（ADR-0019）は技術検査に合格した `final_video` を保存し、Episode を `render_ready` に駐機させる。
次に要るのは、その完成動画を YouTube へ **private で1回だけ**投稿し、投稿の証拠を残す工程である。

- YouTube Data API の `videos.insert` に冪等キーは無い。応答を失うと「投稿されたか」をこちらが知る手段が
  限られ、素朴な再試行は二重投稿になる（INV-14 / AGENTS.md §9）
- 投稿は課金を伴わないが「1回しか起こしてはならない外部副作用」であり、性質は有料 provider の submit と同じ。
  ADR-0013 の予約台帳はまさにこの問題（呼んだか分からない状態で再送しない）を解くために作った
- 未検証の Google Cloud プロジェクトからの投稿は YouTube 側で private に固定される。こちらも private しか
  送らない（INV-19）。公開は所有者の手動判断
- resumable upload の session URI は短命だが**それ自体が投稿の権限**（持っていれば bytes を送れる）であり、
  OAuth トークンと同じく漏らしてはならない（INV-20）
- OAuth のテストモードでは refresh token が 7 日で失効する。`invalid_grant` は人間の再同意でしか直らない
- Shorts は API の旗ではなく、3 分以内の縦長・正方形の動画に YouTube が自動で付ける分類である

## Decision

**現行の `final_video` を検証し、upload key で予約台帳に1行だけ予約してから resumable upload で private 投稿し、
video id を予約に記録してから `upload_receipt` を保存して Episode を `uploaded` にする。
結果が読めないときは新しい session を開かず、マーカー照合で見つからなければ人間を待つ。**

### 1. Episode 状態（新しい状態を作らない）

- 既存の `uploaded` を使う。遷移 `in_progress --UPLOAD_SUCCEEDED--> uploaded` を足す
  （`UPLOAD_SUCCEEDED` は既存の事象）
- 入口は `render_ready --STAGE_ADMITTED--> in_progress`（ADR-0019 が既に置いた辺）。upload 自身の失敗で止まった
  `needs_work` / `blocked` からの再実行は、記録された入場トークンが upload workflow のときだけ（render と同じ規則）。
  語彙は `contracts/states.py::UPLOAD_ADMISSIBLE_STATUSES`
- `uploaded` からの再投稿は無い（API は 409）。`uploaded` の出口は将来の `METRICS_INGESTED` だけ
- 既存の `approved --UPLOAD_SUCCEEDED--> uploaded` / `approved --NEEDS_INPUT_FAILURE--> blocked` は触らない。
  review gate（`ready_for_review` → `approved`）は将来の工程で、今回の経路はそれを通らない。
  **投稿が private に限られる（§6）ので、公開前の人間確認は YouTube Studio 側で残っており、この迂回は安全**

### 2. 語彙（唯一の宣言元 `contracts/states.py`）

| 名前 | 値 |
|---|---|
| `JobType.UPLOAD_FINAL_VIDEO` | `upload_final_video` |
| `ArtifactType.UPLOAD_RECEIPT` | `upload_receipt` |
| `ProviderCall.YOUTUBE_UPLOAD` | `youtube_upload` |
| `UPLOAD_WORKFLOW` | `("UploadWorkflow", "upload")`（`UPLOAD_TASK_QUEUE` はここから導出） |
| `UPLOAD_MEDIA_TASK_QUEUE` | `upload-media`（投稿 Activity だけ。並行数 1） |
| `UPLOAD_ADMISSIBLE_STATUSES` | `render_ready` / `needs_work` / `blocked` |

いずれも Episode 単位（`scene_id` は NULL）。0004 の scene_scope CHECK の凍結集合に入らないので張り替えない。
`video_metadata` Artifact は作らない（§5）。

AGENTS.md §8 の grep（`apps/ workers/ domain/ infrastructure/ contracts/ docs/`、変更前の a51bc13 で実行）:

| キー | 件数 | 分類 |
|---|---|---|
| `upload_final_video` | 0 | — |
| `upload_receipt` | 4 | 無関係 3（components.md の worker 表 2 / data-flow.md の図 1。いずれも予定の記述で本 ADR で更新）・読み手 0・書き手 0、artifact.md の予定表 1（本 ADR で更新） |
| `youtube_upload` | 7 | 無関係 7（legacy-asset-inventory.md の旧repo `youtube_uploader/` の棚卸し。部分一致） |
| `UploadWorkflow` / `UPLOAD_MEDIA_TASK_QUEUE` / `upload-media` | 0 / 0 / 0 | — |
| `UPLOAD_RECEIPT` | 0 | — |
| `UPLOADED` | 3 | 書き手 3（`contracts/states.py` の定義 1、`domain/episode/transitions.py` の既存の辺 2）。意味は変えない |
| `UPLOADS_PAUSED` | 1 | 読み手予定 1（failure-policy.md §7）。`.env.example` に既存。本 ADR で意味を確定（§11） |
| `uploads_paused` / `youtube_channel_id` / `YOUTUBE_CHANNEL_ID` | 0 / 0 / 0 | — （設定キーは adapter の実装で足す） |

### 3. 二重投稿の防止: 予約台帳（ADR-0013 の再利用）

- **upload key** = `sha256(canonical_json({stage: "upload", episode_id, final_video_sha256, destination}))`
  （`domain/upload/keys.py::compute_upload_key`）。`destination` はチャンネル id。版・attempt・run・時刻を**含めない**。
  同じ動画を同じチャンネルへ送る限り同じキー
- 予約の `idempotency_key` = upload key。`UNIQUE(idempotency_key)` と行ロックが並行 attempt を直列化する
- **自動で開くラウンドは1つだけ**。2 ラウンド目を自動で開かない。同じキーで新しい投稿を許す方法は、運用者が
  `OPERATOR_ABANDONED` で予約を放棄することだけ（§8）
- 予約の列の使い方（新しい意味は `provider_result_ref` だけ）:

| 列 | upload での意味 |
|---|---|
| `provider_job_ref` | resumable session URI（§4 の short-lived capability）。session 開始の直後に1度書く |
| `dispatched_at` | **最初の bytes（PUT）を送る直前に** commit。NULL なら bytes を1つも送っていない |
| `provider_result_ref` | YouTube video id。0006 で追加。**受領 Artifact を書く前に**書き、同時に `SPENT` |
| `reconciled_by` | `upload_response` / `status_query` / `marker_lookup`（受領の `reconciled_by` と同じ値を使う） |
| `outcome_artifact_id` | `upload_receipt` の artifact id |

`provider_result_ref` を足すのは、video id を得た後・受領を書く前に crash したとき、YouTube を再度呼ばずに
受領を作れるようにするため（`raw_output_key` は生出力のオブジェクトキーで意味が違う。`provider_job_ref` は
session URI が入っていて上書きすると再照会できない）。

### 4. 投稿の状態機械（1 予約あたり）

1. 予約（idempotency_key）。`SPENT` で `provider_result_ref` があれば、YouTube を呼ばずに受領を作る/再利用する
2. session が保存されていなければ session を開始する（**session 作成だけでは動画は作られないので繰り返してよい**）。
   session URI と総 bytes を予約に保存し、`dispatched_at` を commit してから最初の bytes を送る
3. チャンクを送る（`DEFAULT_UPLOAD_CHUNK_BYTES` = 8 MiB、256 KiB の倍数）。heartbeat に offset を載せる。
   5xx・通信失敗は status query（`Content-Range: bytes */TOTAL`）→ 308 の Range から再開
4. 200/201 → video id を予約に書き `SPENT` → 受領 Artifact → Episode `uploaded`
5. session が 404（失効）/ 送信後に結果が読めない → uploads playlist でマーカー（§7）を探す
   （`DEFAULT_UPLOAD_MARKER_LOOKUP_ATTEMPTS` 回、`DEFAULT_UPLOAD_MARKER_LOOKUP_DELAY_SECONDS` 間隔）。
   見つかれば成功（`marker_lookup`）。見つからなければ `UploadOutcomeUnknownError`（needs_input → `blocked`）。
   **新しい session を開かない**。
   例外: session は保存済みだが `dispatched_at` が NULL（bytes を1つも送っていない）で 404 なら、同じラウンドの中で
   session を開き直してよい（予約に記録する）
6. どこで crash しても、再実行は予約行を読み、保存済み session の status query から続ける

**session URI の扱い（受容したリスク）**: session URI は DB の予約行（`provider_job_ref`）にだけ置く。ログ・例外の
要約・Artifact・API 応答・heartbeat の詳細・トレース属性に出さない（adapter が redact する）。DB を読める者は
失効までの間（最長約 1 週間）bytes を送れるが、送れるのは**この予約が作った private の1本だけ**で、既存動画の
変更・公開はできない。DB 自体を secret と同等に扱う現行の運用で受容する。

### 5. メタデータ（`contracts/upload.py::YouTubeVideoMetadata`）

- title ≤ 100 文字、description ≤ 5000 UTF-8 bytes、いずれも `<` `>` を含まない。tags の合計 ≤ 500 文字
  （空白を含むタグは引用符 2 文字、区切り 1 文字を数える。`youtube_tags_length`）、`,` を含まない
- `category_id`（既定 `DEFAULT_YOUTUBE_CATEGORY_ID = "22"`）、`default_language`（台本の言語）、
  `privacy_status: Literal["private"]`、`self_declared_made_for_kids`、`contains_synthetic_media`（AI 生成なので true）、
  `notify_subscribers: Literal[False]`
- **決定的に導出する**（`build_youtube_metadata`）: title = 台本の title、description = hook + ナレーション
  （`<>` は全角へ置換、5000 bytes で文字境界切り）、tags = 投稿マーカーだけ。別 Artifact（`video_metadata`）は作らず、
  **送った値のスナップショットを受領に入れる**

### 6. private のみ（INV-19）

`privacy_status` はメタデータでも受領でも `Literal["private"]`。public / unlisted はスキーマで表現できない。
`videos.update` / `videos.delete` を呼ぶコードを置かない（アーキテクチャ検査で endpoint を infrastructure/youtube に限る）。

### 7. 投稿マーカー

`upload_marker(upload_key)` = `avpu` + upload key の先頭 24 hex（28 文字、空白なし）。タグとして送り、結果が読めないときに
`channels.list(mine=true, part=contentDetails)` → uploads playlist → `playlistItems.list` → `videos.list(part=snippet)` で照合する。
照合は read-only scope で行う。

### 8. 運用者の放棄手順（同じ動画を投稿し直したいとき）

1. YouTube Studio で、そのチャンネルに該当動画（タグ `avpu…`）が**無い**ことを目視で確認する（あれば削除はせず、
   `OPERATOR_CONFIRMED_SPENT` と video id で照合する）
2. 該当予約を `OPERATOR_ABANDONED` にする（予約は消さない。台帳は履歴）
3. 予約が終端になったので、新しい予約は round 2 になる。upload の POST を再実行する（自動では開かない）

### 9. Shorts / 長尺に中立

upload は profile を知らない。Shorts の旗は API に無く、判定は YouTube が動画の形から行う。契約に Shorts 固有の欄を置かない。

### 10. 既定値（`contracts/upload.py` が唯一の宣言元）

`DEFAULT_UPLOAD_CHUNK_BYTES`（8 MiB）/ `DEFAULT_UPLOAD_MAX_ATTEMPTS`（3）/ `DEFAULT_UPLOAD_HEARTBEAT_TIMEOUT_SECONDS`（60）/
`DEFAULT_UPLOAD_HEARTBEAT_INTERVAL_SECONDS`（10）/ `DEFAULT_UPLOAD_MARKER_LOOKUP_ATTEMPTS`（3）/
`DEFAULT_UPLOAD_MARKER_LOOKUP_DELAY_SECONDS`（30）/ `DEFAULT_UPLOAD_CONCURRENCY`（1）/
start_to_close は `upload_timeout_seconds(size)`（15 分 + サイズ ÷ 256 KiB/s）。
Activity 名と入出力は `contracts/upload_activities.py`。

### 11. 失敗の分類（`domain/errors.py`、failure-policy.md）

| 例外 | クラス | 事象 |
|---|---|---|
| `UploadInputMissingError` | `needs_input` | 現行 final_video / 台本の参照か本体が無い |
| `UploadIntegrityError` | `permanent` | final_video の sha256 がメタデータ・契約と不一致。YouTube を呼ばない |
| `UploadsPausedError` | `needs_input` | `UPLOADS_PAUSED`。session 開始前に止める |
| `UploadAuthError` | `needs_input` | `invalid_grant` / forbidden / youtubeSignupRequired / authorizationRequired（401 は1度 refresh してから） |
| `UploadQuotaExceededError` | `retryable` | quotaExceeded / uploadLimitExceeded / rateLimitExceeded（長い backoff） |
| `UploadRejectedError` | `needs_input` | invalidTitle 等のメタデータ・動画の拒否 |
| `UploadOutcomeUnknownError` | `needs_input` | 送信後に結果不明でマーカーも見つからない |

retry を使い切ったら `blocked`（terminal にしない）。cancel は送信を止め、session を残して `blocked`（再開可能）。

### 12. OAuth scope

`https://www.googleapis.com/auth/youtube.upload`（投稿）と `https://www.googleapis.com/auth/youtube.readonly`（マーカー照合）
だけ。`youtube` / `youtube.force-ssl` などの広い scope は要求しない（既存動画を変更できる権限を持たない）。

## Alternatives

- **(a) 新しい状態 `uploading` を作る**: 却下。`in_progress` + 入場トークンで足り、状態を増やすと表・CHECK・API が割れる
- **(b) 専用テーブル `upload_attempts`**（data-flow.md の旧案）: 却下。予約台帳が同じ意味論（呼んだか分からない状態で
  再送しない・人手照合）を既に持つ。2 つ目の台帳は「未照合の検査」を割る
- **(c) 冪等キーに final_video の version を入れる**（INV-14 の旧文言）: 却下。同じ bytes を再描画で version だけ
  上げると別キーになり二重投稿を許す。sha256 を使う
- **(d) 結果不明なら新しい session で送り直す**: 却下。二重投稿を作る。マーカー照合で見つからなければ人間を待つ
- **(e) `video_metadata` を別 Artifact にする**: 却下（今回）。台本から決定的に導出でき、送った値は受領に残る。
  人手編集が入る段階で再検討する
- **(f) session URI を保存しない**: 却下。crash 後に再開できず、bytes 送信後の crash が全て「結果不明」になる

## Consequences

- migration 0006: `jobs.type` / `artifact_metadata.artifact_type` / `provider_reservations.provider` の CHECK 張り替え
  （Phase 5 の値を literal で凍結）と `provider_reservations.provider_result_ref` 列の追加
- 受領の内容に時刻を入れない（投稿時刻は予約の `reconciled_at` と artifact の `created_at`）。再照合しても同じ bytes
- INV-14 の文言を upload key（sha256）へ改める
- 自動で2回目は投稿しない代わりに、結果不明は必ず人間を呼ぶ（`blocked`）。頻度はマーカー照合で下げる
- 検査（契約）: `tests/contract/test_upload_contracts.py`、`tests/contract/test_migration_frozen_vocabulary.py::test_0006_upgrade_adds_exactly_the_phase6_values`。
  worker / adapter / API の検査は実装のコミットで入れる

## 陳腐化条件

- YouTube API が冪等キー（または client 指定の request id）を提供した → マーカー照合を置き換える
- 公開（public 化）を自動化する所有者の判断が出た → INV-19 を改め、この ADR を置き換える
- `video_metadata` を人間が編集する工程ができた → §5 を改める
- 複数チャンネルへ投稿する → destination の語彙を設定 id へ広げる
