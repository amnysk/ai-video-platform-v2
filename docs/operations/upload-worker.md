# Upload Worker の運用

Status: **Accepted (2026-09-15)**（ADR-0020）

現行の `final_video` を検証し、YouTube へ **private で1回だけ**投稿して `upload_receipt` を保存し、
Episode を `uploaded` にする worker。公開（public / unlisted）は所有者が YouTube Studio で手動で行う（INV-19）。

## 1. OAuth の準備（初回のみ）

1. Google Cloud で OAuth クライアント（種類: デスクトップアプリ）を作り、YouTube Data API v3 を有効にする
2. scope は `youtube.upload` と `youtube.readonly` だけ（ADR-0020 §12）
3. 同意して refresh token を **repo の外の 0600 ファイル**へ書く:

```bash
export YOUTUBE_CLIENT_ID=... YOUTUBE_CLIENT_SECRET=...
.venv/bin/python scripts/youtube-oauth.py   # ブラウザで同意（loopback + PKCE）
```

テストモードのアプリの refresh token は 7 日で失効する。失効すると投稿は `UploadAuthError`（`needs_input` → `blocked`）
で止まるので、同じ手順で同意し直してから再開する（§4）。

## 2. 環境変数

| 変数 | 必須 | 備考 |
|---|---|---|
| `YOUTUBE_CLIENT_ID` / `YOUTUBE_CLIENT_SECRET` | ○ | secret は表示しない |
| `YOUTUBE_REFRESH_TOKEN_PATH` | ○ | repo 外・0600 でなければ起動しない |
| `YOUTUBE_CHANNEL_ID` | ○ | `UC...`。upload key の destination に入る（変えると別キー） |
| `UPLOADS_PAUSED` | | `true` なら YouTube を一切呼ばずに `UploadsPausedError`（`blocked`）。worker の再起動で反映 |
| `YOUTUBE_CHUNK_BYTES` | | 既定 `DEFAULT_UPLOAD_CHUNK_BYTES`（8 MiB、256 KiB の倍数） |
| `AI_VIDEO_WORK_ROOT` | | 本体を試行ごとの作業領域へ落として sha256 を照合する。成功・失敗とも片付ける |

設定が欠けている・token ファイルの権限が違う場合、worker は Temporal に繋ぐ前に理由だけを表示して終了する。

## 3. 起動と実行

```bash
docker compose --profile core up -d --wait
./scripts/run-upload-worker.sh               # 別ターミナル
curl -X POST http://localhost:8000/episodes/<episode_id>/upload   # 本文なし。202
curl http://localhost:8000/episodes/<episode_id>                   # uploaded / upload_receipt
```

1プロセスが2つの Worker を持つ: task queue `upload`（UploadWorkflow・状態系 Activity）と `upload-media`
（投稿 Activity だけ、並行数 1）。

API: `render_ready` から 202。upload 自身が止めた `needs_work` / `blocked` は 202（再開）。`uploaded` は 409（再投稿しない）。
他工程が止めた `needs_work` / `blocked` と、実行中の二重起動も 409。

## 4. 二重投稿を防ぐ仕組み（INV-14）

upload key = sha256(stage, episode_id, final_video の本体 sha256, channel id) を予約台帳（`provider_reservations`、
provider `youtube_upload`）の `idempotency_key` にする。1 予約の中で:

1. 予約が `spent` + `provider_result_ref`（video id）→ YouTube を呼ばず受領を作る / 再利用する
2. session を開始 → session URI を `provider_job_ref` へ commit → `dispatched_at` を commit → 最初の bytes
3. crash・一時障害の後は必ず保存済み session の status query から続ける
4. 完了 → video id と `spent` を commit → 受領 Artifact → `uploaded`
5. `dispatched_at` の後に session が失効したら、uploads playlist でマーカー（タグ `avpu` + key 先頭 24 hex）を探す。
   見つからなければ `UploadOutcomeUnknownError` で `blocked`。**新しい session は自動で開かない**

`dispatched_at` が入った予約の session は条件付き UPDATE で二度と差し替わらないので、bytes を受け取る session は
予約あたり1つだけになる。加えて:

- Episode の状態遷移は compare-and-set（同じ状態を読んだ2つの入場のうち進めるのは1つ）
- 投稿 Activity は session 開始と dispatch の直前に入場トークンを確かめ、所有者でなければ YouTube を呼ばずに降りる
- 送信中も数チャンクごとに予約を読み直し、閉じられていたら止まる
- dispatch を実際に立てた試行だけが offset 0 から送り、他は status query で受理位置を確かめる
- status query の 404/410 は、少し待った2回目の照会でも失効のときだけ失効とみなす
- マーカーはタグと description の最終行の両方に入り、照合はどちらかで一致すればよいsession URI は DB の予約行にだけ置き、ログ・受領・API 応答に出さない（INV-20）。

## 5. blocked の扱い

| `blocked_reason` の型 | 対応 |
|---|---|
| `UploadAuthError` | §1 の同意をやり直し、POST で再開 |
| `UploadQuotaExceededError`（retry 使い切り） | quota の回復（翌日）を待って POST で再開 |
| `UploadsPausedError` | `UPLOADS_PAUSED=false` で worker を再起動し POST |
| `UploadIntegrityError` | final_video が壊れている。render をやり直す（YouTube は呼んでいない） |
| `UploadRejectedError` | 台本の title 等を直してから render / upload をやり直す |
| `UploadOutcomeUnknownError` | 下の手順（結果不明） |

### 結果不明（`UploadOutcomeUnknownError`）の手順

POST で再開しても同じ予約の照合を繰り返すだけで、新しい投稿はしない。人間が確かめる:

1. 対象の予約を見る（session URI は表示しない列だけを選ぶ）:

```sql
SELECT id, round, status, dispatched_at, provider_result_ref, reconciled_by, idempotency_key
FROM provider_reservations
WHERE provider = 'youtube_upload' AND episode_id = '<episode_id>'
ORDER BY round;
```

2. YouTube Studio でチャンネルの動画を開き、タグ `avpu` + `idempotency_key` の先頭 24 文字（ラウンド 1）を持つ動画を探す。
   **削除はしない**
3. 動画が**ある**とき: その video id（11 文字）を記録して spent にし、POST で再開する（YouTube は呼ばれず受領が作られる）:

```sql
UPDATE provider_reservations
SET status = 'spent', provider_result_ref = '<video_id>',
    reconciled_by = 'marker_lookup', reconciled_at = now()
WHERE id = '<reservation_id>' AND status = 'reserved' AND provider_result_ref IS NULL;
```

4. 動画が**無い**とき（処理中で見えないだけの可能性があるので、数時間おいて再確認してから）:
   1. その Episode の upload workflow が**走っていない**ことを確かめる（走っている間は承認しない）:

      ```bash
      temporal workflow describe --workflow-id episode-<episode_id>-upload   # Status が Running でないこと
      ```

   2. 予約を放棄し、**同時に再投稿を承認する**（予約は消さない）。`dispatched_at` が入った予約は、ただの放棄
      （`reconciled_by` が `operator:<name>`）では次のラウンドを開かない。承認は `reconciled_by` の値で記録する:

      ```sql
      UPDATE provider_reservations
      SET status = 'abandoned', reconciled_by = 'operator_reupload_approved', reconciled_at = now()
      WHERE id = '<reservation_id>' AND status = 'reserved' AND provider_result_ref IS NULL;
      ```

   3. POST で再開する。round 2 は投稿の前に**もう一度マーカー照合**を行い、見つかればその video id を記録して
      投稿しない（`reconciled_by = marker_lookup`）。見つからなければ新しい session で投稿する

`dispatched_at` が NULL の予約（bytes を1つも送っていない）は、ただの放棄でも次のラウンドへ進める
（その場合も round 2 はまずマーカー照合をする）。

いずれも `WHERE ... status = 'reserved'` で1行だけ更新されたことを確かめる（0 行なら状態が変わっている。読み直す）。
自動の処理が `abandoned` や承認を書くことは無い（ADR-0013 / INV-15）。

### 別の final_video・チャンネルの投稿が既にあるとき

同じ Episode に、別の upload key（final_video の sha256 か `YOUTUBE_CHANNEL_ID` が違う）の予約が `spent`、または
`reserved` で `dispatched_at` 入りのものがあると、upload は `UploadOutcomeUnknownError` で止まる
（再描画・チャンネル変更で同じ Episode を2本目として投稿しない）。意図した再投稿なら、上の手順で古い予約を確認・放棄する。

### チャンネルの照合

worker は起動時に `channels.list(mine=true)` のチャンネル id が `YOUTUBE_CHANNEL_ID` と一致することを確かめ、
違えば起動しない。投稿の直前にも（worker ごとに1回）確かめ、違えば `UploadAuthError`（`blocked`）。

## 6. 検査

- 二重投稿しない: `tests/unit/test_upload_activities.py::test_concurrent_attempts_on_the_same_key_create_one_video`、
  `tests/unit/test_upload_activities.py::test_session_expired_after_bytes_without_marker_blocks_and_never_reopens`
- private だけを送る: `tests/unit/test_upload_activities.py::test_upload_creates_one_private_video_receipt_and_spent_reservation`
- 実環境（PostgreSQL / MinIO / Temporal + fake uploader）: `tests/integration/test_upload_workflow_persistence.py`
