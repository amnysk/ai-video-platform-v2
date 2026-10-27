# OpenSearch 停止からの復旧と、メモリ保護・監視の導入（ADR-0059）

この文書の**コマンドはすべて REQUIRES OWNER APPROVAL**（本番ホスト = compose project `avp2` / `avp2-logging` に変更を加える）。
エージェントは実行しない。読み取りだけの確認（`docker ps` / `inspect` / `logs`・`monitor-watch.py --show-alerts`・
`log-recoverability.py`）は承認なしで行える。

背景: 2026-10-21 00:02 に 1 サンプルのメモリ低下で OpenSearch が止まり、4 日間誰も気付かなかった（ADR-0059）。
原則: **`docker start` / `docker stop` を OpenSearch の 1 コンテナにだけ使う。** `docker compose up/down`、`-v`、
`rm`、`prune`、他コンテナの再作成は使わない（設定の再評価で他が再作成されうる）。volume（`avp2-logging_osdata`）は保持する。

## 1. 復旧の前提条件（測定した時点で全て成立）

| # | 条件 | 確認 |
|---|---|---|
| 1 | MemAvailable >= **4096MiB が 30 秒間隔で連続 3 サンプル** | `for i in 1 2 3; do awk '/MemAvailable/{print int($2/1024)" MiB"}' /proc/meminfo; sleep 30; done` |
| 2 | メモリ PSI が静か | `cat /proc/pressure/memory`（full の avg10・avg60 が 1.0 未満） |
| 3 | **avp2 の全 worker・temporal・postgres・minio が healthy** | `docker ps -a --filter label=com.docker.compose.project=avp2 --format '{{.Names}} {{.Status}}'`（unhealthy・starting・Restarting・異常終了があれば不可。正常終了(0)の one-shot は可） |
| 4 | 起こすのは **OpenSearch のみ**（`avp2-logging-opensearch-1`）。Dashboards は使うときだけ別に起動 | — |
| 5 | データ volume が残っている | `docker volume inspect avp2-logging_osdata`（14MB 程度。消えていたら起動しない） |
| 6 | 他のコンテナを再作成しない | コマンドは `docker start` 1 本だけ |
| 7 | pytest / temporal-test-server / integration の実行中でない（測定値が数 GiB 振れる） | `ps -eo pid,rss,args --sort=-rss | head` |

根拠: OpenSearch の `mem_limit` 2.5GiB（heap 1GiB + 余り）+ 停止線 1.5GiB の余裕 = 4GiB。heap は `-Xmx1g` のまま
（14MB のデータに十分。`docker start` は環境変数を変えられない。下げるには recreate が要り、別承認）。

## 2. 手動の復旧手順

```bash
export DOCKER_HOST=unix:///run/user/1000/docker.sock
free -m; cat /proc/pressure/memory                                   # 前提 1・2 を記録
docker ps -a --format '{{.Names}} {{.Status}}' > ~/avp-before.txt     # 基準（事後比較用）
docker start avp2-logging-opensearch-1                               # 唯一の状態変更
# healthy を待つ（start_period 60s。通常 1〜3 分。15 秒毎、5 分で諦めて §4 の rollback）
docker inspect -f '{{.State.Health.Status}}' avp2-logging-opensearch-1
# 確認（読み取り専用。--enforce-memory は付けない）
deploy/logging/scripts/check-pipeline.sh --env prod
python3 scripts/monitor-watch.py --show-alerts
docker ps -a --format '{{.Names}} {{.Status}}' | diff ~/avp-before.txt -   # OpenSearch 以外が変わっていないこと
```

期待: cluster yellow/green、Fluent Bit の chunk が 0 へ向かう、app の lag が縮む。既知の finding: `infra: 文書が1件も無い`
（infra 系列が元から空）は別の問題で、この復旧の成否ではない。

Fluent Bit が停止中に破棄した chunk は戻らない。戻せる範囲は §6 で調べる。

## 3. 中止条件（1 つでも当てはまったら起動しない・起動後なら §4）

- 前提 1〜7 のどれかが不成立（特に worker が 1 つでも不健全）
- 起動後 5 分で healthy にならない / OpenSearch が再び OOM・exit で落ちる
- 起動後に MemAvailable が 1.5GiB を下回って回復しない（guard が再び止める。手で粘らない）
- 起動により **OpenSearch 以外**のコンテナの状態が変わった（`diff` が空でない）

## 4. rollback

```bash
docker stop avp2-logging-opensearch-1        # 起動前の状態（停止）に戻る。volume・設定は無傷
```

Fluent Bit を止める判断（人間）: `FLUENT_BIT_CHUNK_AGE` が crit（retry 窓の 80% = 約 9 時間）なのに復旧できないときだけ、
`docker stop avp2-logging-fluent-bit-1`（buffer は volume に残り、位置 DB から再開できる。再開後は retry が数え直しになる）。
json-file は 20MB x 3 の rotation なので、止めたままにできる時間は書き込み量で決まる。止める前に §6 の report で確認する。
再開は `docker start avp2-logging-fluent-bit-1`。

## 5. 監視（OpenSearch が落ちていても動く）

- guard（30 秒毎）: メモリ・OpenSearch の状態・Fluent Bit の buffer / chunk 年齢 / 破棄 / 稼働を見て警報を出す
- host-process-monitor（1 分毎）: PID・zombie・cgroup pids。同じ heartbeat・警報口へ出る
- monitor-watch（5 分毎・別 timer）: 上 2 つの heartbeat が古い・無いと crit
- 見る場所: `journalctl --user -u avp-opensearch-guard -u avp-monitor-watch -u avp-host-monitor | grep avp-alert`、
  `python3 ~/.local/lib/avp-host/monitor-watch.py --show-alerts`（有効な警報の一覧。終了コード 1=crit・3=warn のみ）、
  `systemctl --user --failed`、履歴 `~/.local/state/avp-host-monitor/alerts.jsonl`
- 警報の種類: `OPENSEARCH_STOPPED` / `OPENSEARCH_STOPPED_LONG`（30 分超）/ `OPENSEARCH_DOWN_UNMANAGED`（guard 以外が止めた）/
  `RESTART_BLOCKED` / `RESTART_CAPPED` / `FLUENT_BIT_BUFFER_NEAR_LIMIT`（70%・90%）/ `FLUENT_BIT_CHUNK_AGE`（窓の 50%・80%）/
  `FLUENT_BIT_DROPPED` / `FLUENT_BIT_NOT_RUNNING` / `MEMORY_LOW`（1 サンプルの低下。止めない）/ `HEARTBEAT_STALE:*`・`HEARTBEAT_MISSING:*`
- **人の目に届く通知先（メール・チャット）は未整備**（ADR-0059 の未決事項）。上は journal とファイルまで。

## 6. 取り戻せるログの調査と backfill

```bash
# 読み取りのみ。窓は OpenSearch が止まった時刻から現在まで（UTC）
python3 scripts/log-recoverability.py --since 2026-10-20T15:02:00Z --until "$(date -u +%FT%TZ)" --project avp2
# backfill 用に窓内の行を複写（元の json-file は変更しない。出力は 0600）
python3 scripts/log-recoverability.py --since ... --until ... --project avp2 --out ~/avp-backfill-20261025
```

コンテナごとに recoverable / unrecoverable の範囲が出る（2026-10-25 時点の実測: temporal-1 の 10-20T15:02Z..10-24T13:41Z は
rotation で消えていて unrecoverable）。再投入（OpenSearch へ書く）は **REQUIRES OWNER APPROVAL** で、このリポジトリは実行しない。
再投入の注意: app 系列は `event_id` を `_id`・`write_operation: create` にしているので、同じ行の再投入は 409（成功扱い）で
重複しない。infra 系列は内容の hash が `_id`。再投入手段（隔離した使い捨て Fluent Bit に複写ファイルを tail させる等）は
本番に当てる前に隔離環境で検証する（未検証。この文書は手順を保証しない）。

## 7. 導入手順（すべて REQUIRES OWNER APPROVAL）

systemd unit を置く・timer を有効にする・本番コンテナの設定を適用する操作は所有者が実行する。

### 7.1 コピーを置く（checkout / worktree を消しても止まらない）

```bash
install -D -m 0644 scripts/avp_alerts.py            ~/.local/lib/avp-host/avp_alerts.py
install -D -m 0755 scripts/opensearch-guard.py       ~/.local/lib/avp-host/opensearch-guard.py
install -D -m 0755 scripts/monitor-watch.py          ~/.local/lib/avp-host/monitor-watch.py
install -D -m 0755 scripts/host-process-monitor.py   ~/.local/lib/avp-host/host-process-monitor.py
install -D -m 0755 scripts/log-recoverability.py     ~/.local/lib/avp-host/log-recoverability.py
# 試し打ち（何も変更しない）
python3 ~/.local/lib/avp-host/opensearch-guard.py --dry-run --state-dir /tmp/avp-guard-trial; echo "exit=$?"
```

### 7.2 timer（guard は停止のみ。自動起動は §7.3 で別に判断）

```bash
install -D -m 0644 deploy/host-monitor/avp-opensearch-guard.service ~/.config/systemd/user/
install -D -m 0644 deploy/host-monitor/avp-opensearch-guard.timer   ~/.config/systemd/user/
install -D -m 0644 deploy/host-monitor/avp-monitor-watch.service    ~/.config/systemd/user/
install -D -m 0644 deploy/host-monitor/avp-monitor-watch.timer      ~/.config/systemd/user/
# host-process-monitor を入れるなら docs/operations/host-process-monitor.md（ExecStart が ~/.local/lib/avp-host/ になった）
systemctl --user daemon-reload
systemctl --user enable --now avp-opensearch-guard.timer avp-monitor-watch.timer
systemctl --user list-timers 'avp-*'
```

確認: 数分後に `~/.local/state/avp-host-monitor/heartbeat-opensearch-guard.json` が 30 秒毎に更新され、
`alerts-current.json` に `OPENSEARCH_DOWN_UNMANAGED`（現在の停止。guard が止めたものではない）が出る。
**旧 `avp-logging-check` が動いたままでも、`--enforce-memory` は廃止（何も止めない）済み**の script を使えば二重に止めない。

### 7.3 自動起動（opt-in。別の判断）

`~/.config/systemd/user/avp-opensearch-guard.service` の `ExecStart=` の末尾に ` --auto-restart` を足して
`systemctl --user daemon-reload`。上限は 24 時間に 3 回（`--max-restarts-24h`）、backoff は 600s x 2^回数。
上限後は止めたまま `RESTART_CAPPED`（crit）。外すときは `--auto-restart` を消して daemon-reload。

### 7.4 旧 timer の整理

旧 `avp-logging-check.service` の `ExecStart` から `--enforce-memory` を外す（付けたままでも何も止まらない）。
旧 unit は `.worktrees/claude-daily-hardening` の script を指しているので、その worktree を消す前に
新しいコピー（checkout 済みの `deploy/logging/scripts/check-pipeline.sh`）へ向け直す。

### 7.5 Fluent Bit の retry_limit 288 を本番へ適用

設定の変更は commit 済み（`deploy/logging/fluent-bit/fluent-bit.yaml`）。適用には Fluent Bit の再起動が要る:
`docker compose -f deploy/logging/compose.logging.yaml up -d --no-deps fluent-bit`（**Fluent Bit だけ**。事前に
`docker compose ... config` で差分を確認し、`--no-deps` で opensearch 等が再作成されないことを確かめる。
OpenSearch が停止中でも buffer は volume に残る）。ロールバックは yaml を戻して同じ手順。

### 7.6 全体の rollback

```bash
systemctl --user disable --now avp-opensearch-guard.timer avp-monitor-watch.timer
rm -f ~/.config/systemd/user/avp-opensearch-guard.{service,timer} ~/.config/systemd/user/avp-monitor-watch.{service,timer}
rm -f ~/.local/lib/avp-host/{opensearch-guard,monitor-watch,log-recoverability}.py
rm -rf ~/.local/state/avp-host-monitor/{guard-state.json,heartbeat-*.json,alerts-*.json,alerts.jsonl}
systemctl --user daemon-reload
```

guard を外すと、メモリ逼迫での停止も復旧も無くなる（旧 `--enforce-memory` も廃止済みなので保護が無い状態になる）ことに注意。
