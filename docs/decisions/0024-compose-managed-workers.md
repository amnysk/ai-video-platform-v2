# ADR-0024: 常駐 Worker を Docker Compose で管理する

## Status

Accepted (2026-09-15)

## Context

Phase 1-7 の worker（script / storyboard / production 系 / render / upload / pipeline）は、それぞれ
`scripts/run-*-worker.sh` を別ターミナルで起動する **host プロセス**だった。Daily Schedule（ADR-0023）で
毎日自動で回すには、次が必要になる:

- 再起動・ホスト再起動のあとに人手なしで戻る（落ちたら再起動、ただし設定不足での高速ループはしない）
- 「Temporal に poll している」ことを health として観測できる
- worker ごとに必要な秘密・host 資源だけを渡す（最小権限、INV-18）
- 固定版の外部ツール（Codex CLI / piper-tts / static ffmpeg）を再現可能に揃える

## Decision

1. `compose.yaml` の `core` profile に worker を1サービス1 worker で置く。`restart: unless-stopped`
   （`migrate` は `"no"`）。Schedule 登録（`ensure-daily-schedule`）はどのサービスも行わない
2. **worker イメージは1枚**（`Dockerfile` の `worker` target、`avp2-worker:local`）。`base` を継ぎ、
   git・Node.js + Codex CLI（ARG `CODEX_VERSION`）・隔離 venv `/opt/piper/venv` の piper-tts
   （ARG `PIPER_TTS_VERSION`、`scripts/setup-piper.sh` と同版）・`prompts/` を足す。Python 依存は足さない（ADR-0009）。
   `dummy-worker` / `api` / `migrate` は従来どおり `base`
3. 起動は `python -m infrastructure.runtime.worker_entry <module>`。起動直後の失敗は backoff してから
   非0終了する。Temporal への接続は `infrastructure/temporal/connect.py` の再試行つき接続
4. healthcheck は `python -m infrastructure.temporal.poller_check --queue ...`。そのコンテナの identity で
   各 queue に最近 poll した poller があるかを見る。temporalio は実行枠に空きがあるときだけ poll するので、
   Activity 専用の media queue（render-media / upload-media）は見ず状態系 queue で判断し、Activity 専用
   queue だけの worker（production-image / video / voice）は最長の処理より長い max-age で見る。worker は `temporal`（`cluster health` の出力が行全体で SERVING）/
   `postgres` / `migrate` 完了 / MinIO を使うものは `minio` の healthy を待つ
5. 秘密は `${VAR:-}` 補間でだけ渡し、必要な worker だけが持つ（FAL_KEY: image/video、YOUTUBE_*: upload、
   CODEX_*: script/storyboard）。MinIO を使わない pipeline-worker には MinIO の資格情報を渡さない。
   refresh token は値を env に置かず、ro mount したファイルのパスだけ
6. host 資源は bind mount。コンテナ内のパスは compose に固定し、host 側は `*_HOST_*` 系変数（既定は `${HOME}` 相対）:
   ffmpeg ディレクトリ・フォント・Piper 音声・OpenMontage・token ディレクトリは read-only、`~/.codex` は rw、
   作業領域 `AI_VIDEO_WORK_ROOT` は host と同じパス
7. 全 worker は `cap_drop: [ALL]` と `no-new-privileges:true`。Codex の Linux sandbox（bubblewrap）は
   user namespace を作り uid 0 を map するため、Docker 既定の seccomp と `CAP_SETFCAP` 無しでは動かない（実測）。
   **script-worker / storyboard-worker だけ** `seccomp=unconfined` と `cap_add: [SETFCAP]` にする。sandbox は
   read-only を強制することを確認済み（書き込みは拒否される）
7a. 停止: SIGTERM は SIGINT に転送し、asyncio の cancel で Worker を shutdown する。render / upload は
   `graceful_shutdown_timeout` 100 秒（`stop_grace_period` 120 秒より短い）で実行中の Activity を待つ
8. host プロセス方式（`scripts/run-*-worker.sh`）はデバッグ用の代替として残す。**同じ queue に両方を
   同時に立てない**

## Alternatives

- **host の systemd user unit**: 再起動は得られるが、固定版ツールの再現性と health の観測を別に作る必要がある
- **Codex を `--dangerously-bypass-approvals-and-sandbox` で動かす**: seccomp は既定のままにできるが、
  LLM に書き込み可能な環境を渡すことになる。コンテナ境界だけに頼るより sandbox を残す方を選ぶ
- **専用 seccomp profile（unshare/clone を許可）**: 最小権限としてはより良い。profile の保守コストがあるので
  後で置き換える候補とする
- **worker ごとに別イメージ**: 依存は小さくなるが、版の管理が worker 数だけ増える

## Consequences

- `docker compose --profile core up -d` で worker も起動する。CI の integration は host 資源が無いので
  worker を除いたサービスだけを起動する
- 設定不足の worker（例: OAuth 未設定の upload-worker）は `health: starting` のまま約 65 秒ごとに
  backoff 再起動を続けるが、他の worker には影響しない
- rootless Docker では host uid 1000 がコンテナ root に写るため `AVP_UID=0 AVP_GID=0` にする
  （0600 の token・`~/.codex` を読むため。host 上は非特権）。rootful Docker でこの値を使うと本物の root に
  なるので、`make check-docker-uid`（`up` / `workers-up` が実行）が止める
- `docker compose up -d` は `.env` の `COMPOSE_PROFILES=core` で worker も起動する
- PC 再起動後の自動復旧には、Docker がログインなしで起動すること（rootless は linger）と、
  `/mnt/minio-hdd` が先にマウントされていることが前提
- upload / render の smoke は、Temporal に poller が居れば（compose の worker を含む）実行を拒否する
- bind mount 元が無いと Docker が root 所有で作る。token ディレクトリは `make worker-dirs` で所有者が先に作る
- 運用手順: `docs/operations/workers.md`

## 追補（2026-09-20）: デプロイを1つの手順にし、版を label で確認する

### Context

稼働中の worker が3つの git commit のコードで混在していた（`docs/testing/worker-versions.md` の実測表）。
イメージは `avp2-worker:local` の1枚だが、サービスごとの `build` / `up -d <service>` を繰り返したため、
作り直されなかったコンテナは古い image id のまま動き続けた。どの commit のコードかを示す情報も無かった
（label 無し）。render-worker が topic-planner 側の「英語ナレーションを scene 尺に合わせる」修正の前のコードで
動いていたことに、調査するまで気付けなかった。

### Decision

- イメージの最終 stage（`app` = api / migrate / dummy-worker、`worker`）に `ARG GIT_REVISION` と
  `LABEL org.opencontainers.image.revision` を置く。worker は `ENV AVP_GIT_REVISION` も持ち、
  `worker_entry` が起動ログに出す。**`base` stage には置かない**: base の設定が変わると worker stage の
  apt / npm / piper の層が revision ごとに再ビルドされる（実測 約1分、`tests/contract/test_deploy_workers.py`）
- デプロイは `scripts/deploy-workers.sh`（`make deploy-workers`）だけ。dirty 拒否 → infra 確認 → PRE フック →
  ビルド（各イメージ1回）→ migrate → 全アプリサービスを `up -d --no-deps --no-build` → healthy 待ち →
  版の確認 → POST フック（trap で必ず実行）。フックの中身はスクリプトが知らない（呼び出し側が決める）
- `scripts/workers-versions.sh` は STALE・revision 不明・revision 混在・期待 revision との不一致で exit 1

### Alternatives

- **revision を `ENV` で base に置く**: 実装は最短だが上記のキャッシュ無効化が起きる（実測で確認して不採用）
- **`docker compose up -d --build --force-recreate`**: 版の確認と失敗時の後始末が無く、infra まで再作成しうる
- **スクリプトが schedule の pause / unpause を直接呼ぶ**: 責務が混ざる。フック点だけ提供し、中身は運用側に置く

### Consequences

- 良い: 「どの commit のコードが動いているか」を `make workers-versions` とログで確認できる。混在は検知される
- 良い: 途中で失敗しても POST フックが走るので、フックに置いた後始末（例: 一時停止の解除）が残らない
- 悪い: dirty ツリーからは既定でデプロイできない（`ALLOW_DIRTY=1` は revision に `-dirty` を残す）
- 悪い: デプロイ前に infra が healthy である必要がある（`--no-deps` のため）
