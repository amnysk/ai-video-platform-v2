# 常駐 Worker（Docker Compose）の運用

Status: **Accepted (2026-09-15)**（ADR-0024）

worker は `compose.yaml` の `core` profile で常駐する。`.env` の `COMPOSE_PROFILES=core` により、
素の `docker compose up -d` / `ps` / `logs` / `stop` / `down` が infra + api + 全 worker を扱う。
host プロセス方式（`scripts/run-*-worker.sh`）はデバッグ用の代替。**同じ queue に compose と host プロセスを
同時に立てない**（二重 poll になる）。

**Worker の起動と Schedule の有効化は別の操作**（§9）。PC / Docker の再起動で Schedule が作られたり
有効になったりはしない。

## 1. 構成

| service | module | queue | health で見る queue | 追加で渡すもの |
|---|---|---|---|---|
| dummy-worker | `workers.dummy.run_worker` | episode-skeleton | episode-skeleton | （base イメージ） |
| script-worker | `workers.planning.run_worker` | script | script | `~/.codex`（rw）、Codex sandbox 用の権限 |
| storyboard-worker | `workers.storyboard.run_worker` | storyboard | storyboard | `~/.codex`（rw）、OpenMontage（ro）、作業領域、Codex sandbox 用の権限 |
| production-worker | `workers.production.run_worker` | production | production | - |
| production-image-worker | `workers.production_image.run_worker` | production-image | production-image（max-age 45 分） | `FAL_KEY`、作業領域 |
| production-voice-worker | `workers.production_voice.run_worker` | production-voice | production-voice（max-age 15 分） | Piper 音声モデル（ro）、作業領域 |
| production-video-worker | `workers.production_video.run_worker` | production-video | production-video（max-age 45 分） | `FAL_KEY`、作業領域 |
| render-worker | `workers.render.run_worker` | render, render-media | render | ffmpeg ディレクトリ（ro）、フォント（ro）、作業領域 |
| upload-worker | `workers.upload.run_worker` | upload, upload-media | upload | `YOUTUBE_*`、token ディレクトリ（ro）、作業領域 |
| pipeline-worker | `workers.pipeline.run_worker` | pipeline | pipeline | `PAUSED` / `UPLOADS_PAUSED`（DB と Temporal だけ。MinIO の資格情報は渡さない） |

- イメージは1枚（`avp2-worker:local`、Dockerfile の `worker` target）: Codex CLI 0.154.0、
  `/opt/piper/venv` の piper-tts 1.8.0、git。api / migrate / dummy-worker は `base`（`avp2-app:local`）
- 起動は `python -m infrastructure.runtime.worker_entry <module>`。Temporal へは再試行つきで接続する
  （最大 30 秒間隔）。設定不足などで起動直後に落ちたときは backoff（既定 60 秒）してから終了するので、
  `restart: unless-stopped` でも高速ループにならない
- 全 worker は `cap_drop: [ALL]` + `no-new-privileges`。Codex の sandbox（bubblewrap）が uid 0 の map に
  `CAP_SETFCAP` を要するため、script / storyboard だけ `cap_add: [SETFCAP]` と `seccomp=unconfined`（§8）
- Schedule 登録はどのサービスもしない（§9）

### health の意味

`python -m infrastructure.temporal.poller_check`: **そのコンテナ**が queue を最近（既定 120 秒以内）
poll したことを Temporal に問い合わせる。

- Activity 専用の media queue（render-media / upload-media）は見ない。実行枠が埋まる長い描画・投稿の間は
  poll しないため、正常でも古くなる。状態系 queue（render / upload）が生きていることで判断する
- Activity 専用 queue だけの worker（production-image / video / voice）は、最長の処理より長い max-age で見る
- `health: starting` のまま → 設定不足で backoff 中（§3）。`unhealthy` → Temporal に届いていない
- Docker は unhealthy を理由に再起動しない。Temporal が戻れば worker は自分で poll を再開する

## 2. 前提（初回のみ）

1. host 資源を用意する: `./scripts/install-render-ffmpeg.sh`、`./scripts/setup-piper.sh`（音声モデル）、
   Codex にログイン済みの `~/.codex`、OpenMontage checkout
2. `.env` を `.env.example` から作り、「常駐 Worker」節を埋める（`COMPOSE_PROFILES=core` を含む）。
   コンテナ内のパスは compose に固定してあり、`.env` には **host 側の置き場**（`AVP_FFMPEG_DIR` /
   `OPENMONTAGE_HOST_PATH` / `PIPER_VOICES_HOST_DIR` / `YOUTUBE_TOKEN_HOST_DIR` など）だけを書く。
   秘密（`FAL_KEY` / `YOUTUBE_CLIENT_SECRET` 等）は `.env` にだけ置き、compose には書かない
3. **rootless Docker なら `AVP_UID=0` / `AVP_GID=0`**。rootless では host uid 1000 がコンテナの root に写るため、
   コンテナ uid 1000 では host の 0600 ファイル（refresh token、`~/.codex/config.toml`）を読めない。
   コンテナ root は host 上では非特権の uid 1000 のまま。**rootful Docker では使わない**
   （`make check-docker-uid` が止める。`make up` / `make workers-up` は自動で実行する）
4. `make worker-dirs` で token ディレクトリ（既定 `~/.config/avp`、0700）と作業領域を所有者権限で作る
5. OAuth（upload-worker を動かす場合だけ）: `docs/operations/upload-worker.md` §1。token は
   `$YOUTUBE_TOKEN_HOST_DIR/$YOUTUBE_REFRESH_TOKEN_FILE`（既定 `~/.config/avp/youtube-refresh-token`、0600）
6. **ログインなしで Docker が起動すること**（PC 再起動後の自動復旧に必要）:
   rootless なら `loginctl show-user $USER -p Linger`（`Linger=yes`）と
   `systemctl --user is-enabled docker`（`enabled`）。rootful なら `systemctl is-enabled docker`
7. **`/mnt/minio-hdd` が Docker より先にマウントされること**。未マウントのまま起動すると、MinIO と作業領域の
   bind mount が root ディスク上の空ディレクトリを指す。`/etc/fstab` で起動時にマウントする
   （必要なら docker の unit に `RequiresMountsFor=/mnt/minio-hdd`）

## 3. 起動・状態確認・ログ

```bash
make check-docker-uid              # rootful Docker で AVP_UID=0 を使っていないか
docker compose build               # イメージ（初回・コード更新時）
docker compose up -d               # 起動（infra + api + 全 worker）
docker compose ps                  # 状態。worker は healthy になるまで 30〜60 秒
docker compose logs -f --tail=100 render-worker          # 1つの worker のログ
docker compose logs -f --tail=100 pipeline-worker upload-worker
make workers-logs                  # 全 worker のログ
```

- 起動ログの目印: `... listening on ...`（poll 開始）。`backing off 60s before exit` は設定不足
- 設定が足りない worker（例: `FAL_KEY` 未設定の production-image / video、OAuth 未設定の upload-worker）は
  約 65 秒ごとに backoff 再起動を続け、`health: starting` のまま healthy にならない
  （RestartCount は 150 秒で +2 程度）。他の worker と基盤は影響を受けない。
  `docker compose up -d --wait` はこれらで失敗するので、`--wait` を使うならサービスを列挙する
- 検証: `./scripts/smoke-workers.sh`（`SMOKE_RESTART=1` / `SMOKE_DOWN=1` で再起動シナリオ。有料 queue には投げない）

## 4. 個別再起動

```bash
docker compose restart render-worker     # 再起動（実行中の Activity は最大 100 秒待ってから止める）
docker compose up -d render-worker       # .env / compose を変えたとき（コンテナを作り直す）
```

- `docker kill` は Docker にとって手動停止なので `restart: unless-stopped` でも再起動しない
  （`docker compose up -d` で戻す）。プロセスのクラッシュや OOM kill では自動で再起動する
- 停止しても Workflow の状態は Temporal にある。別のプロセスが続きを実行する
  （`tests/integration/test_worker_restart_durability.py`: 途中 kill でも Episode・投稿は1つ）

## 5. 全体停止

```bash
docker compose stop        # 停止（コンテナ・データは残る。docker compose start / up -d で戻る）
docker compose down        # コンテナを削除（volume・MinIO の実データは残る）
```

- **`docker compose down -v` は使わない**（PostgreSQL の volume = Episode・台帳・Temporal の Schedule が消える）
- `stop_grace_period` は通常 60 秒、render / upload は 120 秒。worker は SIGTERM で poll をやめ、
  実行中の Activity を render / upload は最大 100 秒待つ。待ちきれなかった描画・投稿は、次の起動で
  Temporal が再実行・再開する（投稿は保存済みの session から再開し、2 本目を作らない）
- 新しい仕事を止めたいだけなら、止めるのは worker ではなく §6 のスイッチ

## 6. pause（新しい Episode の生成と自動投稿を止める）

DB スイッチは worker を再起動しなくても次の確認で効く（ADR-0021）。host の venv から実行する。
`DATABASE_URL` は host から見た接続先（`localhost:5432`）にする。

```bash
export DATABASE_URL="postgresql+psycopg://avp:<POSTGRES_PASSWORD>@localhost:5432/avp"
.venv/bin/python scripts/operational-switch.py show
.venv/bin/python scripts/operational-switch.py set paused on --reason "maintenance"
.venv/bin/python scripts/operational-switch.py set paused off
```

- `paused` on: Daily trigger は Episode を作らない（その日の分は作られない。ADR-0023）。投稿ゲートも止める
- 走っている工程は止まらない（Temporal UI で cancel する）
- env の `PAUSED=true` でも止まる（DB と OR。env を変えたら `docker compose up -d pipeline-worker`）

## 7. upload pause（投稿だけ止める）

```bash
.venv/bin/python scripts/operational-switch.py set uploads_paused on --reason "..."
.venv/bin/python scripts/operational-switch.py set uploads_paused off
```

- upload-worker は session 開始前と送信中に確認して止まる（保存済み session は解除後に再開する）。
  pipeline の投稿ゲートも新しい UploadWorkflow を起動しない
- env の `UPLOADS_PAUSED=true` でも止まる（DB と OR）

## 8. Codex sandbox について

Codex は Linux sandbox（bubblewrap）で read-only に閉じて動く。bubblewrap は user namespace を作るため、
Docker 既定の seccomp では `bwrap: No permissions to create a new namespace`、`cap_drop: [ALL]` だけでは
`bwrap: setting up uid map: Operation not permitted` で失敗する（実測）。そのため **script-worker /
storyboard-worker だけ** `seccomp=unconfined` と `cap_add: [SETFCAP]` にしてある（他の cap は不要なことを確認）。
sandbox 内の書き込みは拒否されることを確認済み。
確認（モデルは呼ばない）: `docker compose exec script-worker codex sandbox -- sh -c 'touch /app/x || echo blocked'`

## 9. 復旧確認（PC / Docker 再起動後）と Schedule との関係

```bash
docker compose ps                                   # 全 worker が healthy（設定不足の worker は §3）
docker compose logs --since 10m pipeline-worker     # "listening on pipeline"
docker compose exec temporal temporal schedule list --address temporal:7233   # 登録済み Schedule が残っている
```

- 再起動の順序は Docker 任せ（compose の `depends_on` は効かない）。worker は Temporal に繋がるまで再試行し、
  MinIO が未準備なら backoff 後に再起動して、依存が揃えば自動で healthy になる
- **Schedule は Temporal（PostgreSQL）に保存されている**。worker の起動では作られず、有効化もされない。
  登録・一時停止は所有者が明示的に行う（`docs/operations/pipeline-worker.md`）:
  `python scripts/ensure-daily-schedule.py`（dry-run）→ `--apply` / `--pause` / `--unpause`
- **catchup window は 1 時間**。06:00 の実行時刻から 1 時間以上 Docker / Temporal が止まっていた日は、
  その日の分は実行されない（必要なら復旧後に `temporal schedule trigger` で手動実行。上限は日次枠が守る）
