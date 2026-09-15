# Pipeline worker と Daily Schedule（ADR-0023）

毎日 1 本（`DAILY_EPISODE_LIMIT`）の Episode を自動生成し、Script → Storyboard → Production → Render →
（投稿ゲート）→ Upload まで進める。順序は Temporal workflow だけが持つ（INV-4 / INV-5）。

## 起動

```bash
docker compose --profile core up -d --wait
# 工程の worker を先に起動（別端末）
./scripts/run-script-worker.sh
./scripts/run-storyboard-worker.sh
./scripts/run-production-worker.sh   # + image / voice / video
./scripts/run-render-worker.sh
./scripts/run-upload-worker.sh
# pipeline worker（queue pipeline）
./scripts/run-pipeline-worker.sh
```

## Schedule の登録

```bash
python scripts/ensure-daily-schedule.py            # dry-run: 登録内容を表示するだけ
python scripts/ensure-daily-schedule.py --apply    # create-or-update（何度実行しても 1 つ）
python scripts/ensure-daily-schedule.py --apply --paused   # 停止状態で登録
```

設定（env / `.env`）:

| 変数 | 既定 | 意味 |
|---|---|---|
| `DAILY_SCHEDULE_ID` | `avp-daily-episode` | Schedule id |
| `DAILY_SCHEDULE_CRON` | `0 6 * * *` | cron（`SCHEDULE_TIMEZONE` で解釈） |
| `SCHEDULE_TIMEZONE` | `Asia/Tokyo` | cron と「1日」の日付のタイムゾーン |
| `DAILY_EPISODE_LIMIT` | `1` | 1 日の上限 |
| `PIPELINE_RENDER_PROFILE_ID` | `shorts_vertical` | Render の出力 profile |
| `PAUSED` / `UPLOADS_PAUSED` | `false` | env の停止スイッチ（DB と OR） |

overlap は SKIP、catchup window は 1 時間。設定を変えたら `--apply` を再実行する（入力は Schedule に保存される）。

## 止める

| 止めたいもの | 操作 |
|---|---|
| Schedule 自体 | `python scripts/ensure-daily-schedule.py --pause` / `--unpause` |
| 新しい Episode の生成 + 自動投稿 | `python scripts/operational-switch.py set paused on --reason ...`（または `PAUSED=true`） |
| 自動投稿だけ | `python scripts/operational-switch.py set uploads_paused on`（または `UPLOADS_PAUSED=true`） |

状態表示: `python scripts/operational-switch.py show`。走っている工程は止めない（Temporal UI で cancel）。

## 挙動

- 同じ日の重複 trigger・再実行は上限を超えない（DB の日次枠 / ADR-0021）。子を起動する前に落ちた trigger の
  `planned` Episode は次の trigger が再開する
- 工程が駐機点以外（`needs_work` / `blocked` など）を返す・失敗する・同じ工程がすでに走っている → pipeline は
  その場で `stopped` で終わる。再開は従来どおり API から工程を POST する。翌日の trigger は影響を受けない
- 投稿ゲートが拒否する条件: 停止スイッチ、`render_ready` でない、現行 `final_video` が無い、`youtube_upload`
  予約が spent / dispatched。結果は `upload_skipped` と理由
- 結果は Temporal UI の `daily-episode-*` / `episode-{id}-pipeline` の実行結果で確認する
- trigger 時点で `paused` なら、その日の分は**作らない**（後で解除しても同じ日には再試行しない）。
  必要なら解除後に `temporal schedule trigger --schedule-id avp-daily-episode` で手動 trigger する
  （同じ日の上限は DB の日次枠が守る）
- Upload は bytes 受領後に YouTube の processing 結果を確認してから `uploaded` にする（最大 6 時間。
  ADR-0022）。その間 Episode は `in_progress`
- `stopped` で終わった pipeline は再実行されない（同じ pipeline id は失敗時だけ再起動可）。残りの工程は
  API（`POST /episodes/{id}/storyboard|production|render|upload`）で進める
- Schedule の入力に topic は無い。台本は「トピック未指定」で生成される（企画ソースは未実装）
