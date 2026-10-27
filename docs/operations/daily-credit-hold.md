# 日次の事前診断と保留の運用（ADR-0058）

毎朝 06:00 JST の `avp-daily-episode` は**常に発火する**。入口で読み取りだけの診断をして、
その日の*モード*を決める。Schedule を止めない・止める必要もない（`avp-daily-watchdog` は決して
pause しない）。

## 日次の動き

| 状況 | 日次の動き | 見え方 |
|---|---|---|
| Codex の model が必ず失敗する構成（catalog に無い / 許可リスト外 / 固定必須で未固定） | Codex を呼ばずに失敗 | `DAILY_WORKFLOW_FAILED`（原因 `CodexModelPreflightFailed` と model 名が failure_message に残る） |
| fal の breaker が `open` / `half_open` | 企画・台本・絵コンテまで進め、production の前で保留を記録して止まる | pipeline の結果 `outcome=held`、Episode は `storyboard_ready` のまま、保留テーブルに行 |
| `FAL_MONTHLY_BUDGET_USD` が `0` / 未設定 / 不正（ADR-0061） | 企画・台本・絵コンテまで進め、production の前で保留して止まる。fal へは何も送らない（`PaidJobRunner` も同じ判定で submit 前に止める） | pipeline の結果 `outcome=held`、保留の `snapshot.hold_detail.decision_reason` が `budget_disabled` / `budget_unset` / `budget_invalid`、構造化ログ `provider.call.suppressed` |
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
- 保留の表を読めなかったときは ERROR `HOLD_READER_FAILED` を出し、結果の `hold_reader_failed` が true になる
  （検査は従来の停滞判定に倒れ、保留中の Episode が誤報側に出うる。異常種別は増やさない）。
- 保留は解放されなくても、Episode が `blocked` / `storyboard_ready` を離れた（完了・取消など）行は数えない。
  API の `POST /episodes/{id}/resume` は開いた保留がある間 409（解放は `provider-credits.py resume` だけ）。

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

## 予算 0 の運用と、予算を足すとき（ADR-0061）

- `.env` に `FAL_MONTHLY_BUDGET_USD=0` を**明示**する（空・未設定も同じく有料生成はしないが、意図を残すため）。
  予算 0 のあいだ毎朝の日次は保留で**正常終了**する。Schedule・Worker・watchdog は止めない。
- 保留は 2 件で頭打ち。3 日目以降は `outcome=held_backlog`（新しい Episode を作らない）。「今日の動画は無い」は
  `HELD_EPISODES` ログと `held_episodes` に残る。
- 予算を足す手順（所有者が行う。自動では再開しない）:
  1. `.env` の `FAL_MONTHLY_BUDGET_USD` を正の値にする。
  2. 3 つの worker（pipeline / production-image / production-video）を再作成する（予算は起動時に 1 回読む）。
  3. `scripts/provider-credits.py status` で保留・未照合予約を確認する。**未照合予約は残高・予算に関わらず再送されない**。
  4. 保留した Episode を、承認 token つきの `resume`（既存。ADR-0057 §5）で再開する。保留が 2 件のままだと
     日次は新しい Episode を作らない。
- ログの追い方（OpenSearch が止まっていても）: `docker logs avp2-pipeline-worker-1 | grep -E 'provider.call.suppressed|daily gate'`
  （`docker logs avp2-production-image-worker-1` も同じ）。json-file の保持機構に残る。

## 月間ハードキャップ（ADR-0063 / INV-86）

正の `FAL_MONTHLY_BUDGET_USD` は**月間の上限**（USD）。新しい有料予約は `当月の合計 + その見積 <= 上限`
（ちょうど等しいなら許可）のときだけ作られる。判定は予約の INSERT と同じトランザクションで行うので、
並行する Episode / ブランドでも超えない（上限は fal 族で 1 つ。全ブランド共通）。

- **当月の合計**: UTC の月初以降の fal の予約の見積の合計。状態は問わず数える（未 dispatch の `reserved`・曖昧・
  課金前提を含む）。除くのは「受理されなかったことが確実」と確定した行だけ。実額の列は無いので見積で数える
  （実際より多めに出る = 安全側）。実額は fal の請求画面で確認する。
- **上限に達したとき**: 新しい予約は作られず fal へも何も送られない。保留（`WAITING_FOR_CREDITS`、Episode は `needs_input` の
  保留）になり、保留の `snapshot.hold_detail.decision_reason` が `monthly_cap_exceeded`（`month_counted` / `estimate` /
  `cap` / `remaining` つき）。ログは `provider.call.suppressed`（`decision_reason=monthly_cap_exceeded`）。
  見積が不明（`unknown_cost`）のときも同じ経路で止まる。
- **上限を上げる手順**（所有者が行う）: `.env` の `FAL_MONTHLY_BUDGET_USD` を上げる → 3 つの worker
  （pipeline / production-image / production-video）を**再作成**する（予算は起動時に 1 回だけ読む）
  → `scripts/provider-credits.py status` で `month_spend_usd` と保留を確認 → 承認 token つきの `resume`。
  翌月（UTC）になれば合計は 0 から数え直すが、保留は自動では再開しない（`resume` が要る）。
- **予算を 0 にしたとき**: 新規は止まる。進行中の予約（すでに作られた行）は取り消されず、照合は従来どおり。
- **`unknown_cost` で止まったとき**: 当月に見積が NULL の予約行がある。`scripts/reconcile-reservations.py` で
  照合するか、行の見積を所有者が確認して直すまで、上限の下でも新規は通らない。
- **ログで見る**: `docker logs avp2-production-image-worker-1 | grep -E 'fal monthly hard cap|provider.call.suppressed'`
  （起動時に上限を 1 回記録する）。
- **競合・待ち**: 月ロックは予約の INSERT と commit の間だけ保持する（fal の呼び出し中は保持しない）。
  同時の予約は直列化されるだけで失敗しない。ロック待ちが長いときは `pg_locks`（advisory）と長いトランザクションを確認する。
