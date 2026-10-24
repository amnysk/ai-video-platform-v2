# ADR-0057: fal の残高不足は障害ではなく「管理された一時停止」として扱う

## Status

Proposed (2026-10-25)

ADR-0030（認証失敗の分類）の「401/403 は HTTP status だけを根拠にし、本文から原因を断定しない」を
**部分的に更新する**: 本文に文書化された印があるときだけ原因を確定し、無いときは従来どおり断定しない
（認証と決め打たず `UNKNOWN`）。ADR-0030 の認証 incident ゲート自体は 401（認証）に残る。

## Context

残高が尽きた fal アカウントは**ロック**され、API リクエストが拒否される。2026-10 の本番では
`fal_image` の HTTP 403 が22件（最新 2026-10-13）残っているが、本文を捨てていたため
**残高ロックか鍵失効か事後に判別できない**。コードを読むと、残高不足は次の4つの穴を通って
「障害・入力拒否・課金」として誤記録される（基準 `fc5ca07`）。

| # | 場所 | 起きること |
|---|------|------------|
| G0 | `fal_queue.py` 401/403 | 応答本文を捨て `"(credentials)"` とだけ残す。原因を後から特定できない |
| G1 | 401/403 → `ProviderUnavailableError` | ADR-0030 の認証 incident（10分窓・閾値3, `contracts/production_activities.py`）に数えられ、**10分ごとに抑止が解け**、各シーンが submit→拒否→needs_input を繰り返す |
| G2 | 402 など他の 4xx → `ProviderRejectedError` | `paid_job.py` が `input_rejected_by_provider=True` で確定。**入金後も同じ入力が `ProviderRejectedRetryBlockedError`（ADR-0034）で恒久ブロック**される。課金の問題が入力の内容問題として台帳に残る |
| G3 | NOT_ACCEPTED 経路 | 受理されなかった submit を `spent` + `reconciled_by="conservative"` + 見積額で確定する。確実に未課金なのに費用が過大計上される |

加えて本番の台帳に、`fal_video` の予約 `591d1a8b-ab05-4d9b-b8f7-befad36cc2db`（2026-10-04、dispatched・
provider 参照あり・未照合）が1件残っている。**これは自動で再送してはならない**（AGENTS.md §9）。
再開の仕組みはこの予約を決して越えない。

### fal 公式文書の調査（2026-10-25 取得）

| URL | 書いてあること | 書いていないこと |
|---|---|---|
| https://fal.ai/docs/model-apis/errors | 列挙する status は 400 / 422 / 499 / 500 / 502 / 503 / 504。「`type` が一覧に無ければ、非リトライの 422 の validation error として扱い `msg` を見よ」 | 401 / 402 / 403、残高、ロック、課金の語は無い。読み取り専用の残高 API も無い |
| https://fal.ai/docs/model-apis/faq | 「your account is locked and API requests will be rejected」（残高がロックしきい値を下回ったとき）／「Add credits from the billing dashboard to unlock your account」／「Enterprise customers on invoice-based billing are not subject to automatic locking」 | ロック時の HTTP status・本文の形は書かない |
| https://fal.ai/docs/model-apis/authentication | 鍵は preset（`API` / `SERVERLESS` など）で権限が決まる | 401/403 の応答も、不正な鍵の挙動も書かない |

結論: **公式文書に残高不足の構造化コード・本文の形は無い。** 「403 = 残高不足」も「402 = 残高不足」も
文書化されていない。運用者の観測（本文 `User is locked` / `Exhausted balance`）は公式の根拠ではなく、
この ADR はそれを**出所つきの弱い印**として扱う（下記）。残高を読む無課金・読み取り専用の公式 endpoint は
見つからなかった ── よって残高の確認は**所有者の attestation**（承認 token）で行う（下記 §再開）。

## Decision

**残高拒否を、provider アカウント単位の circuit breaker（DB 永続・時間で自動解除しない）と、
Episode を `blocked` のまま構造化して持つ保留（`provider_credit_holds`）で表す。
workflow は終わる。再開は人の承認の下で、既存の Episode resume 経路だけを通る。**

### 1. 分類（`infrastructure/providers/fal_refusal.py`）

HTTP status だけで残高不足とは決めない。

| 条件 | `RefusalKind` | 例外 |
|---|---|---|
| 401/402/403 + 本文に `BALANCE_BODY_MARKERS` の印 | `BALANCE_EXHAUSTED` | `ProviderBalanceExhaustedError` |
| 401 + 印なし | `AUTH_INVALID`（HTTP の意味論 RFC 9110 §15.5.2。fal 固有の文書ではない） | `ProviderUnavailableError`（従来どおり ADR-0030 の経路） |
| 402/403 + 印なし、本文なし | `UNKNOWN` | `ProviderRefusalUnknownError` |

- 印（`exhausted balance` / `user is locked`）は `fal_refusal.py` の1表だけが持ち、各印に出所
  （公式 URL か `observed:`）を持たせる。現状の2つは `observed:`（公式の形式が無いため）。
  弱い根拠でも安全側: 結果は「breaker を開いて人の承認を待つ」で `UNKNOWN` と同じ止まり方になり、
  誤分類で勝手に再開することはない（再開は常に承認 token を要する）。
- `ProviderBalanceExhaustedError` / `ProviderRefusalUnknownError` は `ProviderRefusalError(NeedsInputError)`。
  `ProviderUnavailableError` の子にしない（G1: 認証 incident に数えない）、`ProviderRejectedError` の子に
  しない（G2: `input_rejected_by_provider` を立てない）。
- 401/402/403 の本文要約（`SUMMARY_MAX_CHARS`=300、API キー・token・署名付き URL・query を除く）を例外
  メッセージと台帳の `error_summary` に残す（G0）。queue（submit）と storage（upload/token）の両方で同じ関数。
- `PERMISSION_DENIED` は予約語として契約に残すが、公式の根拠が無いので今は割り当てない。

### 2. 台帳の意味（G2 / G3）

受理されなかったことが確実な拒否（上の2例外）は、予約を `spent` + `reconciled_by="not_accepted"` で
確定する。費用は 0 として数え、`input_rejected_by_provider=False`。`reconciled_by` は自由文字列で
CHECK 制約が無い（`provider_reservations` の CHECK は `provider` / `status` / `failure_class` のみ）ので、
**既存の制約は広げない**。費用の区分は `domain/provider/cost.py` の純粋関数
（`estimated` / `provider_accepted` / `confirmed_charged` / `not_charged` / `unknown`）で導く。
曖昧（送信後の切断・5xx・request_id 欠落）は従来どおり `reserved` + dispatched のまま未照合に残し、
自動で再送しない（INV-15 / INV-67）。

### 3. breaker（`provider_breakers`, migration 0018）

- 鍵は `ProviderAccount.key`（`fal:default`）。`fal_image` と `fal_video` は同じアカウント
  （`contracts/provider_health.PROVIDER_FAMILY`）。
- 状態: `closed` → `open`（残高拒否・分類不能の拒否の最初の1件で。閾値は1）→ 人の承認で `half_open` →
  `closed`。**時間では閉じない**（窓で閉じると G1 と同じ嵐になる）。
- `half_open` は人が承認した上限付き再開（`RESUME_MAX_CONCURRENCY` 件の Episode）。この間に受理された
  submit（provider が request_id を返した）があれば `closed`、再び残高拒否なら `open` に戻る。
- 開く操作は冪等かつ並行安全: 行の INSERT の一意制約と、`UPDATE ... WHERE state = 'closed'`
  の条件付き更新だけ（アプリ側のロックを作らない）。2つの Episode が同時に拒否されても行は1つ。
- 判定は `PaidJobRunner` の予約 INSERT の**前**（`_check_auth_outage_gate` と同じ位置）。open なら
  `ProviderBreakerOpenError`（needs_input）。台帳には何も残らず、provider へは何も送らない。
  Codex・YouTube・他 provider・monitoring・読み取り専用の分析は `PROVIDER_FAMILY` に無いので影響しない。

### 4. 保留: 新しい EpisodeStatus を作らず `blocked` + `provider_credit_holds`

| 案 | 評価 |
|---|---|
| **A. `blocked` + 理由テーブル（採用）** | 状態機械は既存の2行だけを使う: `in_progress → blocked`（`NEEDS_INPUT_FAILURE`）と `blocked → in_progress`（人の `RESUMED`）。`EPISODE_TRANSITIONS`・`STAGE_PARKING_STATUS`・resume の admit 表・API の `EpisodeView` は変わらない。既存の Workflow 履歴は `episode_status == "blocked"` で分岐する（`_stage_failed_log`）ので replay 影響が無い |
| B. 新 `EpisodeStatus.WAITING_FOR_CREDITS` | `episodes.status` の CHECK を作り直す必要がある。0017 と同じ release gate（制約の drop を機械が止める）に当たり、遷移表・resume 計画・watchdog・API・既存 replay 履歴（`ProductionWorkflowResult.status` の文字列）に波及する。得るものは表示だけ |
| C. workflow が sleep して待つ | 7日待てば履歴が伸び、worker 再起動・deploy を跨ぐ長寿命 workflow を作る。残高が戻らなければ永遠に生きる。却下 |

`blocked` の既存の保証（「通報される。放置されない」）をそのまま使う。理由は
`provider_credit_holds` に構造化して持つ: `episode_id` / `scene_id` / `account_key` / `reason`
（`HoldReason`）/ `held_at` / `resumable_stage` / `generation_profile_id` / スナップショット
（その時点の予約 id と成功済み Artifact id）/ `released_at`。未解放の保留は
(episode, scene, account) で一意（冪等）。

**Workflow コードは変えない。** 例外は `NeedsInputError` なので、既存の経路 ── Activity が
`ApplicationError(type=<型名>, non_retryable=True)` を送出 → workflow が型名で `needs_input` に分類 →
`record_failure` が Episode を `blocked` にして workflow が**終わる** ── に乗る。Activity の retry 嵐も
長時間 sleep する workflow も無く、7日保留しても履歴は伸びない。保留の行は `PaidJobRunner` が
Activity 内で書く（image / video の submit が共通で通る）。したがって `workflow.patched()` が要る分岐の変更は
無く、記録済みの履歴の replay に影響しない（replay テストで確認）。

### 5. 再開（`scripts/provider-credits.py`）

- `status`: breaker・未解放の保留・未照合予約・当月の支出見積を読み取り専用で表示。
- `half-open` / `close`: 構造化された所有者承認 token（`OWNER-<yyyymmdd>-<id>`、
  `is_owner_approval_reference`。**認証ではない**。指紋だけ保存）と、残高確認の根拠（`--evidence`、
  所有者の attestation）が必須。公式の無課金・読み取り専用の残高 endpoint が無いので、自動の残高確認はしない。
- `resume`: 明示した episode id か保留の古い順 N 件（N ≤ `RESUME_MAX_CONCURRENCY`）。次のどれかなら
  **拒否**: breaker が `closed`/`half_open` でない・対象に未照合予約がある（`reserved` かつ evidence なし。
  `591d1a8b…` と同じ状態を含む）・当月の見積支出 + 追加見積が上限を超える（`fal_monthly_budget_usd`。
  設定が無ければ拒否）・既に再開が実行中。通過したら**既存の Episode resume 経路**
  （`POST /episodes/{id}/resume` = `build_resume_plan` → `EpisodePipelineWorkflow`）を呼ぶ。新しい workflow は
  作らない。成功済みシーンは再利用ゲート（INV-17 / ADR-0033）が provider へ再送せず再利用する。
- 既定は dry-run。`--apply` でだけ書く。この CLI は provider へ接続しない。

## Alternatives

- **breaker を既存の `provider_auth_incidents` の `kind` 列で表す**（親の初期案）: 時間窓で自動解除する
  モデルがそのまま残り、「人の承認まで閉じない」が表せない。ADR-0030 の意味（認証 incident）も濁る。
  却下。新テーブル。
- **残高不足を 402 と決め打つ**: 公式に書いていない。403 に出ている観測もあり、status だけでは決められない。却下。
- **half-open で時間経過後に自動 probe**: 残高が戻っていなければ probe が課金対象の submit になりうる。
  公式の無課金 endpoint も無い。却下。人の attestation を必須にする。
- **`reconciled_by` や `status` の CHECK を広げる**: release gate が制約の drop を止める（0017 の未解決問題）。
  新しい事実は新テーブル・新列・自由文字列で持つ。
- **Workflow に待機（signal / sleep）を足す**: §4 C。却下。

## Consequences

良い側:
- 残高が尽きても、Activity の retry 嵐・10分ごとの再 submit・恒久ブロック・見積額の過大計上が起きない。
- 他 provider・他 worker・読み取り専用の分析は止まらない。
- 再開は人の承認・予算・未照合予約の3つの関門を必ず通る。二重課金・二重投稿・成功済みシーンの再生成は起きない。
- migration は expand-only（新テーブルのみ）。release preflight の走査を通る。

引き受けた負債（悪い側）:
- 残高不足の印（`BALANCE_BODY_MARKERS`）は公式文書の根拠ではなく観測。fal が本文を変えれば `UNKNOWN` に
  落ちる（breaker は開く = 安全側だが、理由の表示が「残高不足」から「確認待ち」に変わる）。
- 401（認証）は引き続き ADR-0030 の10分窓ゲートで、窓で抑止が解ける（課金は発生しないが submit→拒否を繰り返す）。
  別の作業で breaker に統合できるが、本 ADR の範囲外。
- 残高の自動確認は無い。所有者が入金を確認して token を書く運用になる。`half_open` で再び拒否されれば
  `open` に戻る（そのとき失うのは拒否された submit 1件で、課金は無い）。
- 停止中の blocked Episode は watchdog の `EPISODE_STAGE_STALLED` に載る（ADR-0031）。保留中であることの
  区別は `provider_credit_holds` を読む日次側（ADR-0058）の役割で、この ADR は watchdog を変えない。
- `resume` の予算判定は見積もり（`estimated_cost_usd`）の合計で、fal の実請求とは一致しない。
- 再開後も、sibling が受理済みで未完了の予約（`reserved` + provider 参照）が残っていれば、既存どおり
  人手照合（ADR-0053）が先に要る。
