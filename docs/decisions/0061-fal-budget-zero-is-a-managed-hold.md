# ADR-0061: fal の月間予算が 0 / 未設定でも、日次は失敗せず有料 submit を 0 回のまま正常終了する

## Status

Proposed (2026-10-25)。ADR-0057（breaker・保留・再開）と ADR-0058（日次ゲート・watchdog 分類）の
上に、**ローカル予算**という 3 つ目の「新しい有料 submit を始めない」理由を足す。
状態機械・DB スキーマ・Workflow のコードは変えない。

## Context

所有者は fal に予算を割り当てていない。これは異常ではなく**意図した運用状態**で、予算を足せば
有料の画像・動画生成を再開できなければならない。

PR #18（ADR-0057 / ADR-0058）の実装を基準に確認した結果（`FAL_MONTHLY_BUDGET_USD` の読み手は
`provider-credits.py resume` の 1 箇所だけ）:

| 要件 | PR #18 時点 | 本 ADR |
|---|---|---|
| 予算 0 で有料 submit が 0 回 | **未実装**。breaker は fal が 1 回拒否してから開く（＝予算 0 でも最初の 1 回は fal へ行く） | 実装（submit 前ゲート） |
| 未設定と明示的な 0 の区別 | 未設定でも submit は止まらない（`resume` だけ拒否） | 実装（`BudgetStatus`） |
| 保留して日次を正常終了 | 実装済み（`PAID_HELD` / `PipelineOutcome.HELD`） | 再利用 |
| 保留上限 2 件・3 日目以降は正常スキップ | 実装済み（`SKIP_BACKLOG` / `DailyOutcome.HELD_BACKLOG`） | 再利用（予算 0 でも効くことを 28 日で検証） |
| watchdog が HELD / FAILED を区別 | 実装済み（保留を stall・期限検査から除外、3 日で既存 kind に escalate） | 再利用 |
| 未照合予約を再送しない・完成シーンを再生成しない | 実装済み（INV-67 / INV-17。`PaidJobRunner` の早期 return） | 予算ゲートはその**後**に置く |
| 予算追加後の再開 | 所有者承認 token つき `resume`（予算・未照合・breaker を検査） | 変更なし（承認必須を維持） |
| fal の残高 API | ADR-0057 の調査（公式文書に読み取り専用の残高 endpoint 無し） | Phase 2 へ。実装しない |

## Decision

### 1. 予算の判定（`domain/budget_gate.py`、純粋）

`FAL_MONTHLY_BUDGET_USD` を `evaluate_budget` が次の 3 状態に分ける（`contracts.provider_health.BudgetStatus`）:

| 設定値 | `BudgetStatus` | `decision_reason` | submit |
|---|---|---|---|
| 未設定・空文字 | `UNSET` | `budget_unset` | しない（**無制限と解釈しない**） |
| 負・非数・NaN/Infinity | `UNSET` | `budget_invalid` | しない（起動は落とさない） |
| `0` | `DISABLED` | `budget_disabled` | しない（意図した停止） |
| 正 | `ENABLED` | `budget_enabled` | この関門は通す |

`DISABLED` では fal へ**何も**送らない（submit も残高照会も）。Phase 1 は provider へ一切問い合わせない。

### 2. 3 か所のゲート（どれも予算が許さなければ fal へ行かない）

1. **日次の入口**（`pipeline_daily_gate`）: 保留上限が先、次に予算 → `PAID_HELD`。企画・台本・絵コンテ
   （無課金）は進める。
2. **production の前**（`pipeline_production_gate`）: 保留（Episode 単位、`resumable_stage="production"`）を
   記録して `PipelineOutcome.HELD`。breaker の状態に**関係なく**予算が先（breaker が結線されていなくても止まる）。
3. **`PaidJobRunner.submit`**（`_check_budget_gate`）: 予約・prepare（元画像の upload を含む）・submit の
   **前**。API・再開経路・将来の新しい入口もここを通るので、1 と 2 を迂回しても有料呼び出しは 0。
   位置は breaker ゲートの直前で、再利用（Reused）・受理済み（Submitted）・未照合（stale）の早期 return の
   **後**なので、完成済みシーンは予算 0 でも再生成されず、未照合予約は予算が正になっても再送されない。

3 の例外 `ProviderBudgetHeldError` は `ProviderBreakerOpenError` の子（`NeedsInputError`）。型名は継承から
自動導出される失敗分類表（`FAILURE_CLASS_BY_TYPE_NAME`）に載り、Workflow は既存の `needs_input` 経路で
Episode を `blocked` にして**正常に終わる**。**Workflow コードは変えない**ので replay に影響しない。

### 3. 保留の記録（migration なし）

- `provider_credit_holds.reason` は既存の `waiting_for_credits`（予算の欠如は原因が確定しているので断定してよい。
  `provider_refusal_unknown` ではない）。
- 機械判定値 `budget_status` / `decision_reason` は既存の `snapshot` JSON に `hold_detail` として保存する
  （`CreditHoldWriter.hold_episode(detail=...)`。Protocol に省略可の引数を足しただけ）。同じ値を構造化ログにも出す。
- breaker は**開かない**（予算は provider の拒否ではない）。予算を足したあとの再開関門は resume だけ。

**不採用: `HoldReason` に `BUDGET_DISABLED` / `BUDGET_UNSET` を足す**。`reason` の CHECK を広げる migration が要り、
release 走査（INV-82 / ADR-0060）は宣言の語彙を **UPPER_CASE のみ**に限っている（0018 の語彙は小文字なので
通らない）。通すには INV-82 の走査器自体を緩める必要があり、Phase 1（PR #18 を延ばさない）の範囲を超える。
区別の価値（一覧で予算保留だけを絞る）は Phase 2 で、走査器の拡張とあわせて判断する。

### 4. 予算を足した後の再開（自動 vs 所有者承認）

| 案 | 評価 |
|---|---|
| 自動再開（予算が正になったら日次が再開） | 却下。日次は過去の Episode を再開しない（INV-78）。未照合予約・half_open の枠・同時実行の二重課金を、環境変数の変更だけが引き金になる経路で守れない |
| **所有者承認つき `resume`（採用・既存を維持）** | 承認 token・予算の上限・未照合予約・既存の再開経路を必ず通る。予算の追加は「新しい日次が新しい Episode を作れる」ことだけを意味する |

保留が `MAX_HELD_EPISODES` に達していると日次は新しい Episode を作らないので、予算を足したあとは
所有者が保留を `resume`（または解放）して枠を空ける。

### 5. 観測

予算由来の抑止は構造化ログ `provider.call.suppressed`（WARNING）に `episode_id` / `provider` /
`attributes.{budget_status, hold_reason, submit_allowed, decision_reason, account, scene_id}` を出す
（`workflow_id` / `correlation_id` / `timestamp` は既存の formatter が Temporal の文脈から付ける）。
secret は載せない（API キーは設定値に含まれない。予算の数値そのものも載せない）。ログは既存の
stdout → json-file の保持機構に出るので OpenSearch が止まっていても追える（`emit` / `log_guard` は
ログの故障を業務へ伝播させない / INV-38）。

watchdog は変えない（保留は Episode 単位で分類し、reason を見ない）。

## Phase 2（別 PR）

- ローカルの累計判定: 当月の確定額 + 未確定予約の見積 + 今回の見積 ≤ 上限（`provider_credits.py` の月間集計を再利用）
- fal 残高 API: 公式に読み取り専用の endpoint が無い（ADR-0057 の調査、2026-10-25）。出るまで実装しない。
  出たら timeout・失敗は「保留・無限リトライなし」とし、結果は `provider_balance_status` に残す
- `HoldReason` への予算理由の追加（INV-82 の走査器の小文字語彙対応とあわせる）
- 予算の変更を worker 再作成なしで反映する（現状は起動時に 1 回読む）

## Alternatives

- **予算 0 のとき日次ごと止める**: 企画・台本（無課金）まで止まり、予算が付いた日に何も用意が無い。却下（ADR-0058 (a)）
- **`HoldReason` に予算理由を足す**: §3。INV-82 の走査器の変更が要る。Phase 2
- **fal へ 1 回 submit して拒否で breaker を開く（PR #18 の現状）**: 予算 0 で最初の 1 回が fal へ行く。却下
- **自動再開**: §4。却下

## Consequences

良い側:
- 予算 0 / 未設定の日は、fal へ 1 回も行かず、Daily も pipeline も失敗せず終わる。何週間続いても Episode は高々 2 件
- 新しい DB 列・表・CHECK・状態遷移・Workflow の分岐が無い。migration なし
- 予算を足す手順は「設定値を正にして worker を再作成 → 所有者が保留を resume」で、承認の関門は変わらない

引き受けた負債:
- **本番の `FAL_MONTHLY_BUDGET_USD` は未設定のはず**。本変更の後は未設定も「有料生成をしない」になる
  （従来は fal へ submit していた）。意図した動作だが、`.env` に明示の `0` を置くことを推奨（`.env.example` 参照）
- 予算は worker の起動時に 1 回読む。変更の反映にはコンテナの再作成が要る
- 保留の理由列は `waiting_for_credits` のまま。予算由来か残高由来かは `snapshot.hold_detail` とログで見る
- `PaidJobRunner(budget=None)`（省略も同じ）は「予算未設定」として **fail-closed**（有料 submit を止める。
  2026-10 修正）。テストで生成処理を通すときは `tests.support.production.ENABLED_BUDGET` を明示する。
  `GatePorts.budget=None` は従来どおり「結線なし」でゲート無効だが、有料 submit は必ず `PaidJobRunner` を通る
  ので submit 自体は止まる。本番の入口は契約テストが結線を検査する
- 旧 worker への rollback 時に新形式の Activity 結果（既定値つき追加フィールド）を読めるかは、Temporal の
  dataclass converter が未知キーを無視する前提で、未検証
- 正の予算は Phase 1 では「ゲートを通す」だけ。累計の超過は resume の検査（既存）と fal 側のロックが守る

## §8 の確認（`FAL_MONTHLY_BUDGET_USD` / `fal_monthly_budget_usd` / `fal_budget`、実装時点）

`grep -rn "FAL_MONTHLY_BUDGET_USD\|fal_monthly_budget_usd\|fal_budget" apps/ workers/ domain/ infrastructure/ contracts/ docs/ scripts/ compose.yaml .env.example`
（ADR 自身・テストを除く）: **15 件**。宣言 4（`infrastructure/config.py`）。値の供給 2（`compose.yaml` 1、`.env.example` 1）。
読み手 7（`workers/pipeline/run_worker.py` 3、`workers/production_image/run_worker.py` 1、
`workers/production_video/run_worker.py` 1、`infrastructure/production/provider_credits.py` 1、
`scripts/provider-credits.py` 1）。文書 2（ADR-0057、`docs/invariants.md`）。無関係 0。
予算の判定の語彙は `domain/budget_gate.py` と `contracts/provider_health.BudgetStatus` のみ。
