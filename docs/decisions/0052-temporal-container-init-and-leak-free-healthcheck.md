# ADR-0052: Temporal コンテナを init 付きにし、healthcheck がプロセスを漏らさないようにする

## Status

Proposed (2026-10-21)。所有者の承認で Accepted にする。本番への適用（コンテナ再作成）は別途の承認が要る
（`docs/operations/temporal-container.md` §4）。

## Context

2026-10-21、本番の `avp2-temporal-1` が `unhealthy`（FailingStreak 49,952 = 約5.8日）だった。実測:

- PID 1 は `temporal-server --env docker start`（`HostConfig.Init=nil`）。子の終了を `wait` しない
- コンテナ内の zombie は 17,934 個で、**全て親が PID 1**。comm の内訳は `temporal` 8,969 / `grep` 8,964 /
  `auto-setup.sh` 1。つまり healthcheck `temporal operator cluster health ... | grep -qx SERVING` の2プロセス組
- 生成日時は 9/20-21（約4,500）と 10/9-10（約11,500）に集中、それ以外は日に数十。サーバが遅い/固まった時間帯に
  5秒の timeout で docker が `sh` を殺し、残った `temporal` と `grep` が孤児として PID 1 へ付け替えられ、
  終了しても刈られない
- コンテナの `pids.max` は 17,968（systemd の user サービス既定の TasksMax。コンテナ個別の設定ではない）で、
  `pids.current` は 17,960。上限に達してからは healthcheck が `failed to create new OS thread (errno=11)` で
  毎回落ち、zombie の増加も止まった（10/15 以降ほぼ増えない）。これが「unhealthy のまま戻らない」理由
- サーバ自体は動いていた（ホストから gRPC で `get_system_info` が 1.29.3 を返し、Schedule
  `avp-daily-episode` / `avp-daily-watchdog` は describe で healthy・unpaused、poller も存在）。
  ただし pids の余裕が 8 しかなく、サーバが新しいスレッド/子を作れなくなる危険の手前にある
- 再現（使い捨て compose project、本番と同じ image）: サーバを SIGSTOP して healthcheck を timeout させると、
  現行定義（init 無し）は約13分で zombie 67 個（1回の失敗あたり2個）、回復後も 69 個のまま残る。
  `init: true` を足した定義は 0 個。どちらも SIGCONT で healthy に戻る

## Decision

**`temporal` サービス（`compose.yaml` と `compose.staging.yaml` の両方）は `init: true` で動かし、healthcheck は
パイプを使わず、CLI を docker の timeout より短い `timeout -s KILL 4` で打ち切る。**

```
init: true
test: ["CMD-SHELL", "[ \"$$(timeout -s KILL 4 temporal operator cluster health --address temporal:7233)\" = SERVING ]"]
```

`interval 10s / timeout 5s / retries 30` は変えない。閾値を緩めない（サーバが本当に落ちたときに unhealthy へ
倒れる速さを保つ）。`init: true` は孤児を刈るので、healthcheck が万一また timeout しても zombie は溜まらない。
パイプ無し + KILL timeout は、そもそも孤児を作らない（二重の防御）。

## Alternatives

- **`init: true` だけ**: zombie は防げる（再現で 0）。ただし timeout した CLI が最大で数十秒生き残りうるので、
  healthcheck 側も有界にして原因を2か所で潰す。採らなかったのではなく併用
- **`pids_limit` を設定**: 溢れる場所をコンテナ内に閉じ込められるが、zombie が溜まる原因は直らない。
  かつ上限値の根拠が無い（現状の 17,968 は systemd 既定の副産物）。採らない
- **host の TasksMax を上げる**: 症状を先送りするだけで、zombie は線形に増え続ける。採らない
- **retries / timeout を伸ばす**: 本当の故障の検知が遅れ、原因も直らない。禁止
- **gRPC health probe（`grpc_health_probe`）に替える**: image に無く、追加バイナリが技術選定の追加になる。
  `temporal` CLI は同じ gRPC を叩くので、現時点では不要
- **`latest` を固定**: 別問題（再現性）。本 ADR の範囲外として記録だけする

## Consequences

- 良い: zombie が溜まらず、pids 上限での healthcheck 全滅が起きない。サーバが固まれば従来どおり
  30回連続失敗（約5分）で unhealthy になる（`test_temporal_healthcheck_fails_*`）
- 悪い: `init: true` は PID 1 を `/sbin/docker-init` に変える。`docker stop` のシグナルは init 経由で
  `temporal-server` に転送されるが、**本番へ適用するにはコンテナの再作成が要り、その間 Temporal の gRPC が
  数十秒止まる**。適用は承認制（運用手順に影響を書いた）
- 悪い（負債）: healthcheck 文字列が2ファイルに重複する。`test_prod_and_staging_share_the_same_temporal_health_definition`
  で一致だけは機械検査する。image が `latest` のままである点は残る
- 本番の既存 zombie 17,934 個は、コンテナを作り直さない限り消えない（PID 1 が刈らないため）
