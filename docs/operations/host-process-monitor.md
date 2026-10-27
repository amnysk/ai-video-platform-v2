# ホストの PID / zombie 監視

`scripts/host-process-monitor.py`。2026-10-21 の `avp2-temporal-1`（zombie 17,934 個・cgroup `pids.max`
到達。ADR-0052）の再発を手遅れになる前に知るための**読み取り専用**の観測。修正（`init: true`・
pipe 無し healthcheck）は ADR-0052 が担う。これは「また別の経路で leak したとき」の検知。

## 何をするか / しないか

- する: `/proc/<pid>/stat` の zombie（state=Z）を親 pid ごとに数える。
  `docker-*.scope` の `pids.current / pids.max` を見る（rootless Docker は `user@UID.service/user.slice` 配下）
- しない: kill・signal・`docker` の呼び出し・コンテナの停止や再起動・root 権限。書くのは自分の state
  ファイル（`~/.local/state/avp-host-monitor/state.json`）と、共通の heartbeat・警報（同じ dir の
  `heartbeat-host-process-monitor.json` / `alerts-current.json` / `alerts.jsonl`。ADR-0059 / INV-80）だけ。止める判断は人間が行う
  （自動で止める系は `check-pipeline.sh --enforce-memory` が一時的なメモリ低下の誤検知で OpenSearch を
  4日止めた前例がある）
- 閾値（既定）: 1 つの親に zombie 10 で warn・200 で crit、合計 20 で warn、cgroup は pids 70% で warn・90% で crit
- 終了コード: 0 ok / 1 warn / 2 crit / 3 読めないものがある（cgroup root が無い等。ok と偽らない）
- 出力: 状態が変わったときと 6 時間ごとの heartbeat だけ（1 分毎に動かしても journal を埋めない）

## 診断（読み取り。手動）

```bash
scripts/host-process-monitor.py --json --state-file /dev/null --no-alerts
```

## ホストへの導入（所有者が実行。REQUIRES OWNER APPROVAL）

```bash
# 1. コピーを置く（checkout を消しても止まらない。avp_alerts.py は同じ dir に置く）
install -D -m 0644 scripts/avp_alerts.py ~/.local/lib/avp-host/avp_alerts.py
install -D -m 0755 scripts/host-process-monitor.py ~/.local/lib/avp-host/host-process-monitor.py
# 2. unit を置く（root 不要。--user）
install -D -m 0644 deploy/host-monitor/avp-host-monitor.service ~/.config/systemd/user/avp-host-monitor.service
install -D -m 0644 deploy/host-monitor/avp-host-monitor.timer   ~/.config/systemd/user/avp-host-monitor.timer
# 3. 試し打ち（1 回。何も変更しない）
python3 ~/.local/lib/avp-host/host-process-monitor.py --json --state-file /dev/null --no-alerts; echo "exit=$?"
# 4. 有効化
systemctl --user daemon-reload
systemctl --user enable --now avp-host-monitor.timer
systemctl --user list-timers avp-host-monitor.timer
journalctl --user -u avp-host-monitor -n 5 --no-pager
```

確認: 最初の 1 回で `heartbeat {...}` が 1 行出て、その後は黙る。`systemctl --user --failed` に出ていない。

### ロールバック（全部元に戻る。他の unit には触れない）

```bash
systemctl --user disable --now avp-host-monitor.timer
rm -f ~/.config/systemd/user/avp-host-monitor.service ~/.config/systemd/user/avp-host-monitor.timer
rm -f ~/.local/lib/avp-host/host-process-monitor.py   # avp_alerts.py は他の監視も使うので、全部やめるときだけ消す
rm -rf ~/.local/state/avp-host-monitor
systemctl --user daemon-reload
```

## heartbeat と警報（ADR-0059）

毎回 `heartbeat-host-process-monitor.json` を書き、warn 以上は `alerts-current.json` / `alerts.jsonl` /
journal（`<N>avp-alert ...`）へ出す。**別の timer の `monitor-watch.py`** がこの heartbeat の古さを見る
（監視自体が止まっても気付ける。`docs/operations/opensearch-recovery.md` §5）。診断のときは `--no-alerts`。
