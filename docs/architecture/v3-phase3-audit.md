# v3 Phase 3 本番監査（読み取りのみ）

- 実測: 2026-10-29 16:40Z〜17:00Z（ホスト時計、UTC）。本番 compose project `avp2`、稼働 SHA `e3c9555`、alembic `0018`。
- 手段: `docker ps/inspect/logs`、`docker exec avp2-postgres-1 psql … BEGIN READ ONLY`、`temporal workflow list|show|describe`・
  `schedule list|describe`（読み取り動詞のみ）。本番の DB・MinIO・Temporal・コンテナには何も書いていない。
- 隔離環境（`AVP_APPTEST_PROJECT=avp2-oslog-c-p3a`）は本番と別 network・別 volume・host ポートなし。
- 秘密（API キー・トークン）は読んでいない・書いていない。

## 0. 結論（重大度つき）

| # | 重大度 | 所見 | 根拠 |
|---|---|---|---|
| 1 | **MAJOR（セキュリティ）** | 変更系を持つ面が LAN に無認証で公開: API `:8000`（本枝で認証を実装）、**Temporal `:7233`・Temporal UI `:8233`・PostgreSQL `:5432`・MinIO `:9000/:9001` も `0.0.0.0`**。Temporal に直接 workflow を起動できる者は API 認証を迂回して有料生成を始められる。本枝のコードでは閉じられない | §4、`ss -ltn` / `docker ps`（`0.0.0.0:7233->7233` ほか） |
| 2 | **MAJOR（課金）** | 月初（11/01 00:00Z）に上限が戻ると、**保留行を持たない `blocked` Episode 15 件**（保留機能 0018 より前の 9/21〜10/13）は、無認証の `POST /episodes/{id}/resume` で再開できる。再開は所有者承認・同時 1 件の制約（`provider-credits.py resume`）を通らない。上限（$60/月）が唯一の歯止め | §3.3、`episodes.py:487`（保留がある Episode だけ 409） |
| 3 | MAJOR（運用） | 保留中の 2 Episode は月が替わっても**自動再開しない**。日次は保留 2 件 = `MAX_HELD_EPISODES` で `SKIP_BACKLOG` のまま。所有者が再開か取消をするまで新しい動画は作られない（意図どおりの安全側。ただし 11/01 以降も止まる） | §3.2 |
| 4 | MINOR | 上限は**見積の合計**で、fal の実残高を見ない（無料の残高 API は無い）。10/05〜10/13 は実際に `HTTP 403 (credentials)` で止まった | §3.1、watchdog の anomaly ログ |
| 5 | MINOR | 上限に達した日の「漏れ」は小さい（10/28: fal 画像 3 枚 $0.12 + Codex 台本 2・絵コンテ 2）が、10/27 は Episode 1 本の途中で上限に当たり $6.29 の部分生成物が残った。Episode 単位の事前見積（残りが 1 本分未満なら始めない）は無い | §3.4 |
| 6 | INFO | 次の日次（10/29 21:00Z）は**何も生成しない**（保留 2 件で `SKIP_BACKLOG`）。fal 0・Codex 0・Episode 0 | §5 |
| 7 | INFO | デプロイ後の fal・Codex・YouTube の新規呼び出し 0、traceback 0、未照合予約 0。最後の投稿は 10/27 の private のまま | §1・§2 |
| 8 | INFO | 手動の予約確定の旧記録 1 件（`provider_reservation_reconciliations.actor = amnysk`）は種別（owner / agent_delegated）を持たない旧形式。ADR-0080 以降は「所有者承認とは読まない」 | §2 |

BLOCKER は無い。#1・#2 は、API 認証（本枝）のデプロイと compose の bind 変更（所有者判断）で閉じる。

## 1. 再検証した状態（親の実測と一致）

- 稼働 SHA: 全アプリコンテナが `e3c9555`（`avp2-worker:local` / `avp2-app:local`、起動から約 16 時間）。alembic `0018`。
- Temporal: Running workflow 0。Schedule `avp-daily-episode`（paused=false、Overlap=Skip、Catchup 1h、次回 21:00Z）と
  `avp-daily-watchdog`（paused=false、毎時 :35）。Daily の起動回数 41。
- 未照合: `provider_reservations` の `reserved` 0、`abandoned` 0、`spent` で `reconciled_at` が空 0。
- 投稿: `episodes.status='uploaded'` 16 件。最後は `48f4eddb`（10/27、private、`paX9vJkJVHs`）。デプロイ後の `youtube_upload` 新規 0。
- 月の fal 集計（10 月）: **59.3627 USD / 上限 60**（画像 3.00 + 動画 56.3627）。残り 0.6373（画像なら 15 枚、動画 0.9676 は不可）。
- 保留: `provider_credit_holds` 4 行（`waiting_for_credits`、`resumable_stage=production`、すべて未解放）。2 Episode:
  `42a07716`（10/27、sb7）と `48993d70`（10/28、sb1/sb2/sb3）。`provider_breakers` は 0 行（breaker は開いていない）。
- `blocked` の Episode は 17 件: 保留行あり 2、保留行なし 15（9/21〜10/13）。`render_ready` が 1 件（`9522d03c`、9/15）。

## 2. (a) workflow 履歴・(b) 予約と照合

### (a) 直近の Daily（Temporal の保持期間は **24 時間**。10/27 の履歴は既に消えている）

| 実行 | 状態 | 内容 |
|---|---|---|
| `daily-episode-2026-10-28T21:00:00Z` | Completed | Activity 4（`pipeline_check_paused` → `pipeline_daily_gate` → `codex_model_preflight` → `pipeline_claim_daily_slot`）。子 workflow 2 起動。失敗 0、再試行 0 |
| `episode-48993d70-…-production`（ProductionWorkflow） | Completed（=失敗を記録して終了） | Activity 17 本、うち **3 本が `ProviderMonthlyBudgetExceededError`**（`counted 59.3627 + estimate 0.9676 vs cap 60; no reservation was created and no paid call was made`）。`maximum_attempts` に達する再試行はなく、非再試行扱い（attempt 2 以上 0）。`production_record_failure` が 1 回 |
| `episode-48993d70-…-pipeline` / `-storyboard` / ScriptWorkflow | Completed | `pipeline_production_gate` 1 回。3 子 workflow 完了 |
| `daily-watchdog-*` | 毎時 Completed | 16 時間分すべて成功 |

10/27 以前の Daily は Temporal から消えている。DB の台帳が唯一の証跡（§(b)）。**この制約は代替監視の限界**: Temporal の履歴は
24 時間で消えるため、事後調査には台帳と構造化ログ（OpenSearch 停止中は docker logs のみ）に依存する。

### (b) 日別・provider 別の予約（10 月、全件 `spent`・全件照合済み）

| 日 | fal_image | fal_video | USD | codex_script / storyboard | 備考 |
|---|---|---|---|---|---|
| 10/01〜10/03 | 各 10 | 各 10 | 11.29 / 11.04 / 11.53 | 各 1〜2 / 1 | 通常の 1 本（投稿済み） |
| 10/04 | 6 | 4 | 7.01 | 1 / 1 | 途中で停止（blocked） |
| 10/05〜10/13（8 日） | 各 2 | 0 | 各 0.08 | 1〜3 / 0〜3 | 画像 2 枚のあと fal が `403 credentials`。毎日同じ型で止まった |
| 10/27 | 22 | 16 | 17.81 | 3 / 2 | `48f4eddb`（投稿、$11.53）+ `42a07716`（途中で上限、$6.29） |
| 10/28 | 3 | 0 | 0.12 | 2 / 2 | `48993d70`。画像 3 枚のあと上限で停止 |
| 10/29（デプロイ後） | 0 | 0 | 0 | 0 | 新規の予約なし |

- 件数: fal_image 188 / fal_video 148 / codex_script 41 / codex_storyboard 35 / codex_scene_alternative 5 / youtube_upload 16。**全て `spent`**。
- 照合の経路: `reconciled_by` = `evidence` 384、`conservative` 32（9 月 18 件 $7.25・10 月 14 件 $0.56）、`upload_response` 16、
  `operator:amnysk` 1（10 月の fal_video $1.9352、`confirmed_failed`、根拠「owner verified in fal dashboard 2026-10-27」、
  承認の指紋あり。旧形式で種別なし）。`reserved`・`abandoned` は 0。**`spent` でない経路は残っていない**。
- 月の再計算: 10/01〜04 = 40.87、10/05〜13 = 0.56、10/27 = 17.81、10/28 = 0.12、合計 59.3627（上限の判定値と一致）。
- 注意: `conservative` 14 件（$0.56）と手動確定の 1 件（$1.9352）も「課金済み」として 10 月の集計に入っている（保守側）。

## 3. (c) 月初（2026-11-01 00:00Z）に何が起きるか

### 3.1 上限

- 集計は `reserved_at >= month_start_utc(now)`（`infrastructure/db/repositories.py:762`、定義 `domain/budget_gate.py:63`）の fal 族の全行
  （`NOT_CHARGED` を除く）。11/01 00:00Z に集計は 0 に戻り、上限 $60 が丸ごと復活する。
- 強制は予約と同じトランザクション内（`repositories.py:1003 _enforce_monthly_cap`、月ロック → 入力ロック）。事前検査
  `paid_job.py:879 _precheck_monthly_cap` は無駄なアップロードを避けるだけで、権威は reserve。
- 上限は**見積の合計**（実残高ではない）。fal に無料の残高 API が無いため、実際の枯渇（10/05〜13 の 403）は見えない。

### 3.2 保留と再開は自動で起きない

- 保留（`provider_credit_holds`）の解放は `PostgresProviderHealth.release_holds`（`provider_health_repositories.py:385`）だけで、呼び手は
  `provider_credits.resume_episodes`（`infrastructure/production/provider_credits.py:305`）= `scripts/provider-credits.py resume`。
  所有者承認の参照（`validate_approval`、`:86`）・入金の根拠・未照合予約 0・`month_spend + estimated_additional ≤ cap` を要求し、
  既定は dry-run。watchdog（`workers/pipeline/watchdog.py`）に再開・解放の呼び出しは無い（保留の年齢は anomaly を出すだけ）。
- API の `POST /episodes/{id}/resume` は**保留行がある間** 409（`apps/api/routers/episodes.py:487`）。
- したがって、保留 2 Episode は 11/01 以降も `blocked` のまま。**自動で再開もせず、課金もしない**。
- 日次の入口: `daily_gate`（`workers/pipeline/activities.py:210`）が未解放の保留を持つ Episode の数
  （`open_hold_count` = `COUNT(DISTINCT episode_id)`、`provider_health_repositories.py:410`）を数え、`>= MAX_HELD_EPISODES(2)`
  （`contracts/provider_health.py:109`）なら `SKIP_BACKLOG`（`domain/daily_gate.py:55`）。Workflow は企画・claim・Codex 診断の**前**に
  終了する（`workers/pipeline/workflows.py:160-170`）。現在 2 件なので、**11/01 以降も日次は新しい Episode を作らない**。

### 3.3 保留を持たない 15 件の `blocked`（MAJOR #2）

- 保留行は 0018 の保留機能より後の停止にだけ付く。9/21〜10/13 の 15 件は保留行なし = `has_live_hold` が偽
  （`provider_health_repositories.py:420`）。API の再開は `plan.resumable` なら通り（`episodes.py:428-480`）、認証が無いので LAN の誰でも呼べる。
  再開された Episode は `production` から有料生成を始め、**同時実行数の上限・所有者承認・予算の事前見積はない**。
- 止まるのは月の上限だけ。Episode 1 本の見積は約 $11.5（10/27 実績 $11.53）なので、11/01 に 15 件を一斉に再開されると、上限
  $60 まで（約 5 本分）が一日で課金されうる。10 月は上限に達しているので現時点では 0 円。
- 本枝の API 認証（ADR-0080 §B）は、デプロイとトークン設定のあとこの経路を閉じる。**デプロイ前に 11/01 を迎えると、この穴が開く**。
  なお、これら 15 件が実際に `resumable` かは未確認（`GET /episodes/{id}/resume/plan` は API 呼び出しなので、本監査では実行していない）。

### 3.4 「複数の保留 Episode + 新しい日次」は同じ日に積み上がるか

- 保留の再開は同時 1 件（`RESUME_MAX_CONCURRENCY = 1`、`contracts/provider_health.py:112`、`resume_episodes` の `_active_resumed_episodes`）。
  日次は 1 日 1 本（`daily_episode_limit`）。つまり正規の経路では最大でも「再開 1 本 + 日次 1 本 ≒ $23」。
- ただし**日別の上限は無い**。歯止めは月の上限（$60）だけで、正規の経路でも 3 日目で月が尽きる。無認証の API 経路（§3.3）は同時実行の制約も受けない。
- これは ADR-0063 が意図した「月間のハードキャップ」であり、日別の上限は設計に無い。意図されていないのは §3.3 の経路。
- 部分生成物の問題（MINOR #5）: 上限は予約ごとに働くので、残りが 1 本分に満たないと途中で止まる（10/27 の `42a07716` は画像 12・動画 6 の
  あとで停止）。Episode の事前見積で残額を見る仕組みは無い。

### 3.5 11/01 から 3 日後に起きること

- 所有者が保留を解く（再開か取消）まで日次は `SKIP_BACKLOG`。watchdog の anomaly（`EPISODE_NOT_COMPLETED_BY_DEADLINE` ほか）は
  毎時 ERROR ログに出続ける（16 時間で 32 イベント、すべて既存の停止の再通知）。保留の年齢が `DEFAULT_HOLD_ESCALATION_DAYS = 3`
  （`contracts/daily_gate.py:22`）を超えると escalation 扱い（通知のみ）。

## 4. (d) ログと露出面

- `docker logs`: 全アプリコンテナは 10/29 00:55Z の再作成からのログだけが残る（それ以前のコンテナのログは再作成で失われ、
  OpenSearch は停止中）。**traceback 0**。`ERROR` 行 64 件はすべて pipeline-worker の watchdog の anomaly 通知（`anomaly.recorded` 32 イベント + 同数の
  `(recorded)` 行。既存の停止の毎時の再通知）。種別は `EPISODE_NOT_COMPLETED_BY_DEADLINE` 16、`EPISODE_STAGE_STALLED` 15、
  `EPISODE_NOT_UPLOADED_BY_DEADLINE` 1。例: Episode `e1b7c0c8` の理由は `fal submit refused: HTTP 403 (credentials)`。
- 予期しない provider 呼び出し: デプロイ後の `provider_reservations` 新規 0（最後の行は 10/28 21:04Z）。API の request ログは healthz の `GET` 1 件のみで、
  変更系の呼び出しはデプロイ後ゼロ。
- 露出面（`ss -ltn` / `docker ps`、すべて `0.0.0.0` と `[::]`）:

  | ポート | サービス | 認証 | 備考 |
  |---|---|---|---|
  | 8000 | API | **無し**（本枝で変更系に bearer を実装） | LAN 192.168.1.9 |
  | 7233 | Temporal gRPC | 無し | workflow の起動・Schedule の変更が可能 |
  | 8233 | Temporal UI | 無し | 画面から workflow を起動・終了できる |
  | 5432 | PostgreSQL | パスワード | `avp`・`avp_test`・`temporal*` が同居 |
  | 9000 / 9001 | MinIO（API / console） | キー | |

  → API 認証だけでは Temporal 経由の起動を防げない。推奨: `compose.yaml` の公開を `127.0.0.1:` にする（所有者判断。この枝では compose を変えない）。

## 5. (e) 次の Daily（2026-10-29 21:00Z）が何をするか

1. `pipeline_check_paused` → paused でない。
2. `pipeline_daily_gate`: 未解放の保留を持つ Episode = **2**（`blocked` の `42a07716` と `48993d70`）≧ `MAX_HELD_EPISODES` 2 →
   `SKIP_BACKLOG`（予算の判定より前、`domain/daily_gate.py:55`）。
3. Workflow は `DailyOutcome.HELD_BACKLOG` で終了。**Codex 診断・企画・claim・Episode 作成・fal・YouTube の呼び出しは 0**。
   残る痕跡は `daily gate … mode=skip_backlog` のログと Workflow の結果（`held_backlog`）だけ。
- 親の予想（「小さな fal_image + Codex を使ってから止まる」）は 10/28 の挙動で、今回は保留が 2 件に達したため起きない。
- 参考: 保留が 1 件以下で上限だけが効いている状態の「漏れ」は 1 日あたり fal_image $0.08〜0.12（2〜3 枚）+ Codex 台本 1〜3・絵コンテ 1〜3
  （Codex は課金額が台帳に出ない）。保留 2 件で漏れは止まる（`MAX_HELD_EPISODES` がその歯止め）。

## 6. 代替監視（OpenSearch は所有者判断で停止のまま。起動していない）

| 見るもの | 手段（すべて読み取り） |
|---|---|
| アプリのログ | `docker logs --since <時刻> avp2-<service>-1` の `Traceback` / `anomaly.recorded` / `provider.call.suppressed` |
| Temporal | `temporal workflow list`・`workflow show`（履歴は 24 時間で消える）・`schedule describe`（paused・次回・件数） |
| 台帳 | `BEGIN READ ONLY` の SELECT: `provider_reservations`（`reserved` 0、日別の fal 件数と USD）、`provider_credit_holds`（未解放数）、`episodes.status` |
| 月の残り | `SUM(estimated_cost_usd)`（`reserved_at >= 月初`、`reconciled_by <> NOT_ACCEPTED`）と上限 60 の差 |

履歴が 24 時間で消えるため、日次の翌日確認が事後調査の唯一の窓になる。長期の検索・集計は失われたまま（Phase 2 報告 §3.3）。

## 7. この枝で行った変更（本番には何も適用していない）

| 範囲 | 内容 |
|---|---|
| integration の本番誤接続 guard | ADR-0079 / INV-121・122。`test_pipeline_schedule.py` の `localhost:7233` を撤去、隔離の宣言が無ければ収集時に例外、本番 Temporal は Schedule の有無で拒否。Postgres・MinIO は宣言と接続先の規則のみ（読み取りだけの識別子が無い） |
| 承認の種別 | ADR-0080 §A / INV-123。`OWNER-…` と `AGENT-…:OWNER-…`、所有者専用の関門、`approval_ref` 必須 |
| API 認証 | ADR-0080 §B / INV-124・125。`docs/operations/api-auth.md` に手順と呼び出し元の棚卸し |

実機の確認（読み取りのみ）: 本番 `localhost:7233` に対し guard が 3 通りとも拒否（宣言なし・偽の CI 宣言→動的検査が `avp-daily-episode` を検出・
隔離宣言+localhost）。隔離スタックでは Temporal 系の integration 5 件が skip なしで通る。本番ホストの PostgreSQL には `avp_test` DB が
実在する（`pg_database`）ので、旧 guard は本番サーバへ書き込めた。
