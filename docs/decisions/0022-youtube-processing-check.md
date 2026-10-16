# ADR-0022: 投稿後の YouTube 処理状態の確認と送信中の一時停止

## Status

Accepted (2026-09-15)

## Context

ADR-0020 の UploadWorkflow は、YouTube が全 bytes を受理して video id を返した直後に Episode を `uploaded` にしていた。

- 受理は「動画が使える」ことを意味しない。YouTube はその後に処理し、`status.uploadStatus` が
  `processed` になるか、`rejected`（duplicate / copyright / termsOfUse など）・`failed`（codec / invalidFile など）・
  `deleted` になる。`uploaded` のまま拒否された動画を「投稿済み」と記録すると、人間が気づく場所が無い
- 公開範囲とチャンネルは投稿時に送った値だが、YouTube 側で実際にそうなったかは確かめていなかった（INV-19）
- `UPLOADS_PAUSED` は session 開始前に1回だけ見ていた。大きな動画の送信中に止める手段が無かった
- `videos.list(part=status,processingDetails,snippet)` は 1 quota unit。`processingDetails` は所有者だけが読めるが、
  既存の `youtube.readonly` scope で足りる（scope を増やさない。ADR-0020 §12）

## Decision

**UPLOAD_FINAL_VIDEO と UPLOAD_MARK_UPLOADED の間に処理状態を照会する Activity `upload_await_processing` を置く。
処理完了・private・投稿先チャンネルのときだけ `uploaded` にし、それ以外は既存の record_failure で `blocked` にする。
待ちは Activity の RetryPolicy で表す。一時停止は送信中も効かせる。**

### 1. 判定（`domain/upload/processing.py::classify_processing`、純粋関数）

| 観測 | 判定 | 失敗クラス |
|---|---|---|
| items が空 / 404（まだ見えない） | `PENDING`（`not_found_yet`） | `retryable`（`UploadProcessingPendingError`） |
| `snippet.channelId` ≠ `YOUTUBE_CHANNEL_ID` | `FAILED`（`channel_mismatch`） | `needs_input`（`UploadProcessingFailedError`） |
| `uploadStatus` = `rejected` / `failed` / `deleted` | `FAILED`（`rejected_<reason>` など） | `needs_input` |
| `processingStatus` = `failed` / `terminated` | `FAILED` | `needs_input` |
| 未知の `uploadStatus` | `FAILED`（INV-12） | `needs_input` |
| `privacyStatus` ≠ `private`（完了時は読めないことも含む） | `FAILED`（`not_private`） | `needs_input` |
| `uploadStatus` = `processed` | `PROCESSED` | — |
| `uploadStatus` = `uploaded` / 処理中 | `PENDING` | `retryable` |

理由コードは識別子として安全な文字だけ（adapter も値を `[A-Za-z0-9_-]` に絞る）。token・URL は運ばない。

### 2. 待ち方

- Activity は1回照会して判定するだけ（heartbeat 不要）。`start_to_close` 2 分
- RetryPolicy: 初回 30 秒・係数 2・最大間隔 10 分・回数無制限・`schedule_to_close` 6 時間
  （`contracts/upload.py::DEFAULT_UPLOAD_PROCESSING_*`）。`needs_input` / `permanent` の型は retry しない
- 期限切れは retryable の使い切りとして `blocked`（`RETRY_BUDGET_EXHAUSTED`）。Episode は処理中ずっと `in_progress`
- 入場トークンを持たない実行は照会せずに `UploadOwnershipLostError` で降りる

### 3. 再開（POST）

`blocked` からの再開は ADR-0020 のまま: admit → 投稿 Activity は予約の `spent` を見て **YouTube を呼ばない** →
処理状態の照会からやり直す。拒否された動画を自動で再投稿することは無い（重複・権利・規約は人間が判断する）。
受領 Artifact の形は変えない（処理状態は workflow の結果にだけ載せる）。

### 4. 一時停止

`UploadActivities.uploads_paused` は async の callable（env `UPLOADS_PAUSED` と、ADR-0021 の operational switch
`uploads_paused` の論理和を worker が組む）。session 開始前に加えて、送信中の予約の再確認
（`reservation_check_every_chunks` ごと）でも見て `UploadsPausedError` で止める。session は予約に残るので、
解除後の POST は保存済み session の受理位置から続ける（2本目の動画は作らない）。

## Alternatives

- **(a) Episode に新しい状態 `processing` を作る**: 却下。状態表・CHECK・API が割れる。処理待ちは upload 工程の中の
  待ちで、`in_progress` + 入場トークンで表せる（ADR-0020 §1 と同じ判断）
- **(b) workflow の timer（`workflow.sleep`）でループする**: 却下。RetryPolicy と `schedule_to_close` で同じことが
  宣言的に書け、履歴も短い
- **(c) 投稿 Activity の中で処理完了まで待つ**: 却下。upload-media queue（並行数 1）を数時間占有し、次の投稿を止める
- **(d) 拒否されたら自動で再投稿する**: 却下。duplicate / copyright は再投稿で直らず、二重投稿（INV-14）を作る
- **(e) scope `youtube.force-ssl` を足して詳細を読む**: 却下。readonly で足りる

## Consequences

- 投稿1本あたりの quota は照会回数ぶん増える（1 unit × 数回〜数十回）
- `uploaded` は「YouTube が private で処理を終えた」ことを意味するようになる
- 検査: `tests/unit/test_upload_processing.py`（判定の網羅）、`tests/unit/test_upload_processing_e2e.py`
  （処理中→完了・拒否→blocked→再開で再投稿なし・チャンネル違い・非公開でない・投稿後の中断からの再開）、
  `tests/unit/test_upload_activities.py::test_pause_during_sending_stops_and_resumes_the_saved_session`

## 陳腐化条件

- YouTube が処理完了の通知（push）を提供し、それを受ける経路を作った → 照会を置き換える
- 公開を自動化する（INV-19 を改める）→ privacy の判定を改める
