# Pipeline worker と Daily Schedule（ADR-0023）

> **常駐運用は compose**（`docker compose --profile core up -d`、`docs/operations/workers.md` / ADR-0024）。
> 以下の host プロセス方式はデバッグ用の代替。同じ queue に両方を同時に立てないこと。

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
| `DAILY_EPISODE_LIMIT` | `1` | 1 日の上限。**`> 1` にしても同じ profile の組では実質 1**（1 日 1 Plan / ADR-0025） |
| `PIPELINE_RENDER_PROFILE_ID` | `shorts_vertical` | Render の出力 profile |
| `PAUSED` / `UPLOADS_PAUSED` | `false` | env の停止スイッチ（DB と OR） |
| `TOPIC_STRATEGY_PROFILE_ID` | `us_young_history_v1` | Topic Planner の strategy profile の **id**（中身は `contracts/topic_planning.py`） |
| `TOPIC_CONTENT_PROFILE_ID` | `shorts` | Topic Planner の content profile の **id** |
| `YOUTUBE_ANALYTICS_ENABLED` | — | YouTube Analytics の live 取得を行うか。無効・失敗時は snapshot / `no_analytics` で続ける |

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
- Topic は Episode を作る前に Topic Planner（ADR-0025）が決める。下の「Topic Planner」を参照

## Topic Planner（ADR-0025）

Daily の流れ: `pipeline_check_paused` → 子 `TopicPlannerWorkflow`（queue `script`、id
`topic-plan-{date}-{strategy}-{content}`）→ `pipeline_claim_daily_slot`（`topic` / `topic_plan_id` 付き）→
`EpisodePipelineWorkflow`。

- Planner は **planning worker（queue `script`）** で動く。導入時は script worker を**再デプロイ**する
  （`docker compose --profile core up -d --build` など。古い worker は `TopicPlannerWorkflow` を知らず、Daily が子で止まる）
- 同じ日・同じ profile の組の Plan は 1 件（INV-22）。再実行は既存 Plan を再利用する。そのため
  `DAILY_EPISODE_LIMIT > 1` でも同じ profile の組では 1 日 1 本に留まる（既知の制約）
- Planner が失敗（全ラウンドで候補が重複・契約違反）すると Daily は失敗し、その日の Episode は作られない。
  原因を直してから手動 trigger する
- Analytics: OAuth に `yt-analytics.readonly` scope が付与されるまでは live 取得が失敗し、Planner は
  `stale_analytics`（保存済み snapshot があれば）/ `no_analytics` で動く。企画は止まらない。採用 mode は
  `topic_plans.analytics_mode` で確認する
- profile を変えるときは env の id を変えて `--apply` を再実行する（profile の中身を env に書かない）
- Planner には execution timeout（`TOPIC_PLANNER_EXECUTION_TIMEOUT_SECONDS`、90 分）がある。Daily は最大その時間
  Planner を待つので、Schedule の overlap SKIP はこの間の次の trigger も捨てる。同じ id の Planner が走っていたときの
  待ち時間（`TOPIC_PLANNER_BUSY_WAIT_SECONDS` × `TOPIC_PLANNER_START_ATTEMPTS`）はこの timeout から導かれ、
  使い切ると Daily は non-retryable で失敗する

### 導入時のデプロイ手順（ADR-0025）

`DailyEpisodeWorkflow` は Planner の導入を `workflow.patched("topic-planner-0025")` で分岐しており、導入前に
始まった実行も旧経路（Planner 無し・入力の `topic` で claim）で replay できる。それでも安全側に倒し、次の順で入れる:

1. `python scripts/ensure-daily-schedule.py --pause`
2. 走行中の `DailyEpisodeWorkflow` が無いことを確認する
   （`temporal workflow list --query 'WorkflowType="DailyEpisodeWorkflow" AND ExecutionStatus="Running"'`）
3. planning worker（queue `script`）と pipeline worker を再デプロイする
4. `python scripts/ensure-daily-schedule.py --unpause`

旧経路の実行が retention 期間を過ぎて残っていないことを確かめてから、patch の分岐（旧経路）を削除できる


### ADR-0026 反映後の注意（台本の再利用）

`script_input_hash` に locale・topic_plan_id・content profile が入ったため、**反映前に作られた Episode を
Script 工程から再実行すると既存台本は再利用されず、Codex で台本を作り直す**（下流の storyboard 以降も古くなる）。
反映前の Episode は Script から再実行しない。進行中の Production / Render / Upload の再開は影響を受けない。
