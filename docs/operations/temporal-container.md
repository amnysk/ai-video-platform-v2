# Temporal コンテナ（init と healthcheck）

ADR-0052。2026-10-21 の本番 `avp2-temporal-1` が zombie 17,934 個で unhealthy になった件の運用記録と手順。

## 1. 診断（読み取りだけ。本番で実行してよい）

```bash
C=avp2-temporal-1; P=$(docker inspect -f '{{.State.Pid}}' $C)
# PID 1 の直下の zombie 数（増えていないか数分おきに）
ps -eo ppid=,stat= | awk -v p=$P '$1==p && $2 ~ /^Z/' | wc -l
# zombie の正体（comm は zombie でも残る）。temporal と grep が同数なら healthcheck の残骸
ps -eo ppid=,stat=,comm= | awk -v p=$P '$1==p && $2 ~ /^Z/{print $3}' | sort | uniq -c
# cgroup の pids（rootless docker: user@UID.service 配下）
D=/sys/fs/cgroup/user.slice/user-$(id -u).slice/user@$(id -u).service/user.slice/docker-$(docker inspect -f '{{.Id}}' $C).scope
cat $D/pids.max $D/pids.current
# サーバが実際に動いているか（ホストから。コンテナ内で CLI を増やさない）
TEMPORAL_ADDRESS=localhost:7233 python scripts/schedule-guard.py status
```

判定: `pids.current` が `pids.max` に近く、zombie の親が PID 1 で comm が `temporal`/`grep` なら ADR-0052 の事象。
`FailingStreak` が大きくても zombie が増えていないのは上限に達して fork できないだけで、治ってはいない。

## 2. 修正の中身

`temporal` サービスは `init: true` と、パイプ無し・`timeout -s KILL 4` の healthcheck（両 compose で同一）。
閾値は interval 10s / timeout 5s / retries 30 のまま。検査: `tests/contract/test_compose_temporal_healthcheck.py`。

## 3. 再現結果（使い捨て project、本番と同じ image、サーバを SIGSTOP）

| 定義 | 約13分後の PID 1 配下 zombie | サーバ停止中 | SIGCONT 後 |
|---|---|---|---|
| 現行（init 無し・パイプ） | 67（回復後も 69 で残る） | unhealthy | healthy |
| `init: true` + 現行 check | 0 | unhealthy | healthy |
| `init: true` + 新 check | 0 | unhealthy（streak 30 で flip） | healthy |

## 4. 本番への適用手順（所有者の承認が要る。このブランチの作業者は実行しない）

影響: `temporal` コンテナを作り直す間、Temporal の gRPC（7233）が止まる（起動と auto-setup で数十秒〜1分見込み。
実測して記録すること）。worker は自動で再接続し、実行中の workflow は永続化された履歴から再開する。
Schedule は PostgreSQL に永続しているので消えない。再作成中に `avp-daily-watchdog`（5分毎）や
`avp-daily-episode`（21:00 UTC）の発火時刻と重ならないようにする。

1. 前提: 直近のバックアップが検証済み（復旧演習に通っている）であること。所有者の明示的な承認を得る
2. 実行中の episode / 外部 API 予約が無い時間帯を選ぶ（`scripts/production-preflight.py`、
   未照合の provider 予約が無いこと — AGENTS.md §9）
3. 本ブランチを main に取り込んだ commit を本番 checkout へ反映する（compose.yaml が変わる）
4. maintenance pause で包んで **temporal だけ**を作り直す（他のコンテナは触らない）:
   ```bash
   scripts/with-maintenance-pause.sh --reason temporal-recreate --ttl 30m -- \
     docker compose --profile core up -d --no-deps --force-recreate --wait temporal
   ```
5. 確認: `docker inspect -f '{{.State.Health.Status}}' avp2-temporal-1` が healthy、PID 1 が `docker-init`、
   zombie 0、`schedule-guard.py status` の両 Schedule が healthy・unpaused、各 queue の poller が存在
   （`scripts/production-preflight.py` / 各 worker の healthcheck が healthy）
6. 失敗時: コマンドが非0なら pause は解除されない（意図どおり）。原因を直して `schedule-guard.py reconcile`
7. `temporal-ui` は `depends_on: temporal` だが `--no-deps` なので再作成されない（UI が一時的に落ちるだけ）
