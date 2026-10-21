# Temporal コンテナの init / healthcheck: 各テストが存在する理由

対象: `compose.yaml` / `compose.staging.yaml` の `temporal`（ADR-0052）。
実測の事故（2026-10-21: zombie 17,934 個・pids.max 到達・healthcheck 全滅）の再発を防ぐ。

| テスト | 防ぐもの |
|---|---|
| `test_temporal_runs_under_an_init_that_reaps_orphans` | PID 1 が `temporal-server` のままに戻り、孤児が zombie として溜まる |
| `test_temporal_healthcheck_has_no_pipeline_and_bounds_the_cli` | `cli \| grep` に戻り、timeout で2プロセスが孤児になる。CLI の打ち切りが docker の timeout より長く、docker に殺されて孤児になる |
| `test_temporal_healthcheck_thresholds_are_not_loosened` | 「落ちるから」と interval/timeout/retries を緩めて故障の検知を遅らせる |
| `test_prod_and_staging_share_the_same_temporal_health_definition` | 片方だけ直し、staging で検証した定義と本番が食い違う（AGENTS.md §8） |
| `test_temporal_healthcheck_passes_only_on_exact_serving` | 正常時に healthy にならない |
| `test_temporal_healthcheck_fails_when_not_serving` | `NOT_SERVING` / 空 / エラー終了を healthy と読む（部分一致・終了コード無視） |
| `test_temporal_healthcheck_fails_within_docker_timeout_when_server_hangs` | サーバが固まったとき CLI が戻らず、healthcheck が docker timeout を越えて孤児を作る（再現条件そのもの） |
