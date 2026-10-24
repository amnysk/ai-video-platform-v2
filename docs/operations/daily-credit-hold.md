# 日次の事前診断と保留の運用（ADR-0058）

毎朝 06:00 JST の `avp-daily-episode` は**常に発火する**。入口で読み取りだけの診断をして、
その日の*モード*を決める。Schedule を止めない・止める必要もない（`avp-daily-watchdog` は決して
pause しない）。

## 日次の動き

| 状況 | 日次の動き | 見え方 |
|---|---|---|
| Codex の model が必ず失敗する構成（catalog に無い / 許可リスト外 / 固定必須で未固定） | Codex を呼ばずに失敗 | `DAILY_WORKFLOW_FAILED`（原因 `CodexModelPreflightFailed` と model 名が failure_message に残る） |
| fal の breaker が `open` / `half_open` | 企画・台本・絵コンテまで進め、production の前で保留を記録して止まる | pipeline の結果 `outcome=held`、Episode は `storyboard_ready` のまま、保留テーブルに行 |
| 未解放の保留が `MAX_HELD_EPISODES`（2）に達した | 何も作らない（Planner・Codex も呼ばない） | Daily の結果 `outcome=held_backlog`、watchdog の `HELD_EPISODES` ログ |
| それ以外 | 従来どおり | |

保留・skip は**成功ではない**（Temporal 上は completed でも動画は無い）。失敗でもない（意図した停止）。
保留した Episode を日次が勝手に再開することは無い。翌日の日次は新しい Episode を作る（上限まで）。

## 保留の見方（watchdog）

- 毎時 :35 の検査が WARNING `HELD_EPISODES count=N escalated=K oldest_days=D` を出す
  （`docker logs avp2-pipeline-worker-1 | grep HELD_EPISODES`）。結果の `held_episodes` /
  `held_escalated` / `oldest_hold_days` も同じ。
- 保留が **3 日**（`DEFAULT_HOLD_ESCALATION_DAYS`）を超えると、既存の `EPISODE_STAGE_STALLED` が
  `detail.hold`（理由・アカウント・開始時刻・再開できる工程）つきで記録され、通常の通知経路に出る。
  `psql` で見るなら `operational_anomalies` の `kind='EPISODE_STAGE_STALLED'` かつ `detail ? 'hold'`。
- 保留が解放されれば次の検査で自動的に閉じる。

## 保留の解放（所有者の作業）

残高の補充または鍵の修正のあと、**所有者が** ADR-0057 の再開経路（breaker の解除と保留の release、同時再開数
`RESUME_MAX_CONCURRENCY`）で行う。日次は再開しない。未照合予約があるときは先に
`docs/operations/reservation-reconciliation.md`（二重課金防止）。

## Codex の復旧（所有者の手順）

詳細は `docs/operations/codex-model-config.md`。要点:

1. 読み取りだけの診断（利用枠を使わない）: `python scripts/codex-model-preflight.py`
   （`unverified` は「使える証明が無い」。`exit 2` は必ず失敗する構成）。
2. 承認のうえ probe（利用枠を 1 回ずつ使う）で、コンテナの codex（0.154.0）が通る model を実測する。
3. `.env` に `CODEX_MODEL` と `CODEX_ALLOWED_MODELS` を実測した model で、`CODEX_REQUIRE_PINNED_MODEL=true`、
   `docker compose up -d script-worker storyboard-worker scene-alternative-worker`。
   共有の `~/.codex/config.toml` は本番の model の出所にしない。
4. 日次を 1 回、承認のうえ手動起動して `topic_generate_candidates` の成功を確認する。

## 結線の状態（導入時の注意）

ゲートは `workers/pipeline/gate_ports.py::build_gate_ports` が返す port を使う。breaker・保留の実装
（ADR-0057）を結線するまでは「breaker は closed・保留 0」として動く（従来どおり）。pipeline worker の
起動ログに `pipeline gates: provider health / credit hold ports are not wired` が出ていれば、保留は効いて
いない。Codex 事前診断は script worker に `codex_model_preflight` Activity があれば効く（無い旧 worker
では 60 秒待って検査せずに進む）。
