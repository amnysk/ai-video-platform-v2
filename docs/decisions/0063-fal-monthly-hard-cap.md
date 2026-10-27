# ADR-0063: `FAL_MONTHLY_BUDGET_USD` を月間のハードキャップにする（予約と同じトランザクションで強制）

## Status

Proposed (2026-10-27)。ADR-0061 が置いた「正の値は関門を通すだけ」を置き換える。
DB スキーマ（migration）・Temporal の Workflow / Activity の署名・直列化される payload は変えない。

## Context

ADR-0061 の `FAL_MONTHLY_BUDGET_USD` は**オン/オフの切替**にすぎない（0 / 未設定 / 不正 = 保留、正 = 通す）。
正の値は何も制限しない。例えば `FAL_MONTHLY_BUDGET_USD=5` にしても、月に何百ドル使っても止まらない
（累計を見る唯一の経路は `provider-credits.py resume` の事前検査で、submit の経路には無い）。
予算ゲート（`PaidJobRunner._check_budget_gate`）と予約の INSERT（`ProviderReservationRepository.reserve`）は
別のトランザクションなので、仮に累計を見ても「検査 → 予約」の間に並行する予約が入ると、上限を超えうる。
お金の事故は二度と起こさない（AGENTS.md §9）ため、上限は予約と**同じトランザクション**で強制する。

## Decision

`FAL_MONTHLY_BUDGET_USD`（正の値）を、**fal 族の当月合計の上限**とする。新しい有料予約は
`month_counted + estimate <= cap`（ちょうど等しいなら許可、超えるなら拒否）のときだけ作られる。

1. **純粋関数**（`domain/budget_gate.py`）: `evaluate_cap(cap, month_counted, estimate) -> CapDecision`。
   `reason ∈ {within_cap, monthly_cap_exceeded, invalid_input, unknown_cost}`。Decimal のみ（float 禁止）。
   負・非有限の入力は `invalid_input` で拒否（fail closed）。`BudgetDecision.cap_usd` は ENABLED のときだけ入る。
2. **月初は 1 つの関数**: `month_start_utc(now)`（UTC の月初・naive）。`provider_credits.month_start` と
   新しい検査が同じ関数を使う（2 回書かない）。
3. **`month_counted` の定義（1 箇所）**: fal 族（`PROVIDER_FAMILY` の `FAL_IMAGE` / `FAL_VIDEO`）の、
   `reserved_at >= 月初` の全行の `estimated_cost_usd` の合計。状態（reserved / spent / abandoned）を問わず数え、
   **費用区分が `NOT_CHARGED` の行だけ除く**（受理されなかったことが確実な拒否で確定した行。`domain/provider/cost.py`
   の `NOT_ACCEPTED_RECONCILER`）。曖昧・課金前提・未照合の行は見積額で数える（安全側）。
   未 dispatch の `reserved`（`ESTIMATED`）も数える: 予約は「これから使う枠」なので、並行する別 Episode が
   同じ枠を二重に使わないために必要。見積が NULL の行が 1 つでもあれば「不明」で拒否（`unknown_cost`）。
   SQL の `SUM` で数える（Python のループではない）。
4. **原子性**: `ProviderReservationRepository.reserve` が、INSERT と**同じ DB トランザクション**の中で
   `pg_advisory_xact_lock(sha256("fal-monthly-cap|YYYY-MM") の先頭 64bit)` を取り、次に既存の入力ロックを取る
   （**固定順序: 月ロック → 入力ロック**。逆順は作らない = デッドロック防止）。その後に合計を読み、
   `evaluate_cap` で判定し、許可のときだけ INSERT する。ロックはトランザクション終了（呼び出し側の commit /
   rollback）で解放される。SQLite（unit test）では advisory lock をスキップするが、合計と判定は行う。
5. **`reserve` の署名**: `monthly_cap_usd: Decimal | None = None, now: datetime | None = None` を足す。
   `None` は上限検査なし（既存の呼び出し・テストの後方互換）。`PaidJobRunner` は予算が ENABLED のとき必ず
   上限を渡す。**fal の `reserve` を呼ぶ本番コードは `paid_job.py` の 1 箇所だけ**で、これを契約テストが固定する
   （上限なしの経路を後から足せない）。
6. **冪等**: 同じ `idempotency_key` の予約が既にある（再試行・Temporal の replay・worker 再起動）なら、
   上限検査をせず既存の冪等の経路（一意制約 → 読み直し）に任せる。その見積は既に `month_counted` に入って
   いるので二重に数えず、上限で拒否もしない。上限検査は**本当に新しい行**にだけ効く。
7. **拒否の経路**: `ProviderMonthlyBudgetExceededError(ProviderBudgetHeldError)` を投げる（`CapDecision` の
   数値を持つ）。予約行は作られず、fal は呼ばれず、クラッシュでもない。`PaidJobRunner` が既存の保留の経路
   （`provider_credit_holds`、`WAITING_FOR_CREDITS`、`needs_input`）に流し、`hold_detail.decision_reason =
   monthly_cap_exceeded`。再開は既存の `scripts/provider-credits.py resume` だけ。ログは既存の
   `provider.call.suppressed` に `decision_reason` / `month_counted` / `estimate` / `cap` / `remaining` を載せる。
   `prepare`（fal CDN へのアップロード）の前に、同じ判定の**非原子的な早期検査**を置いてよい（無駄なアップロードを
   避ける）。権威は `reserve` の中の検査。
8. **見積が出せない・NULL**: 拒否（`unknown_cost`）。上限があるときに費用が分からない予約は通さない。
9. **resume と gate の一致**: `provider-credits.py` の `month_spend_usd`（status / resume）は `month_counted` と
   同じ定義・同じ関数を使う。以前の定義は未 dispatch の `reserved` を 0 と数えていたが、上限側と食い違うと
   「resume は通るのに submit が上限で止まる」ので、数える側に揃える（より安全側）。
10. **運用**:
    - 予算を 0 にしても、`evaluate_budget` は DISABLED で新規を拒否し続ける。**進行中の予約は取り消さない**。
    - 上限を上げ下げするには worker の再作成が要る（予算は起動時に 1 回だけ読む / ADR-0061）。
    - 台帳に brand / account の列は無い。fal 族のアカウントは 1 つ（`default`）なので、**上限は全ブランド・全
      Episode で 1 つ**。並行する Episode / ブランドは同じ月ロックで直列化される。
    - 台帳は見積だけを持つ（実額の列は無い）。見積で数えるのが安全側で、受理されなかったと確定した行は解放される。
      実額との差は fal の請求画面で確認する。

## Alternatives

- **別トランザクションの事前検査だけにする**: 検査と予約の間に並行する予約が入ると超える（TOCTOU）。採らない。
  ただし無駄なアップロードを避ける早期検査としては併用する。
- **行ロック（`SELECT ... FOR UPDATE`）や `SERIALIZABLE`**: 合計は集約なので、守る行が無い。
  advisory lock は既存の入力ロックと同じ流儀で、トランザクション終了で自動解放される。
- **累計を別テーブルのカウンタで持つ**: migration と二重管理（台帳とカウンタのずれ）が生じる。台帳が単一の真実。
- **fal の残高 API で上限にする**: 公式の読み取り専用 endpoint が無い（ADR-0057）。
- **実額で数える**: 実額の列が無く、migration と reconcile の拡張が要る。見積で数えるのは安全側。
- **ブランドごとの上限**: 台帳に brand 列が無い。今回は 1 アカウント 1 上限。必要になったら別 ADR。

## Consequences

良い点:

- 正の予算が本当に月間の上限になる。並行する予約でも超えない（月ロックで直列化）。
- 拒否は管理された保留（失敗でもクラッシュでもない）。台帳にも fal にも何も残らない。再開は既存の承認つき `resume`。
- 冪等な再試行は上限で壊れない。

引き受ける負債:

- **全 fal 予約が月ロックで直列化される**。ロックは INSERT と同じ短い区間（呼び出し側の commit まで）で、
  fal の呼び出し中には保持しない。現在の規模（日次 1 本）では競合は無視できる。
- 見積が実額より大きいと、上限に実際より早く達する（安全側の偏り）。`reserved` のまま未照合の行は、照合されるまで
  枠を占有する（解放は照合の結果で決まる）。
- 見積が NULL の行が月内に 1 つでもあると、その月の新規予約は `unknown_cost` で止まる。解消は照合・見積の修正。
- 上限の変更に worker の再作成が要る（ADR-0061 から引き継ぎ）。
- `month_spend_usd` の定義が変わる（未 dispatch の `reserved` を数える）。status / resume の表示額が増えうる。
- ブランドごとの予算配分はできない。
- 台帳に `(provider, reserved_at)` の索引は無い。月内の行数が小さい間は問題にならない（必要になれば別 ADR）。

## §8 の確認（`FAL_MONTHLY_BUDGET_USD` / `fal_monthly_budget_usd` / `fal_budget`、実装時点）

`grep -rn "FAL_MONTHLY_BUDGET_USD\|fal_monthly_budget_usd\|fal_budget" apps/ workers/ domain/ infrastructure/ contracts/ docs/ scripts/ compose.yaml .env.example`
（ADR-0061 / 本 ADR・`docs/testing/`・テストを除く）を実装時に走らせた結果: **28 件**。

| 分類 | 件数 | 内訳 |
|---|---|---|
| 宣言（設定の定義） | 4 | `infrastructure/config.py` |
| 値の供給（書き手） | 2 | `compose.yaml` 1、`.env.example` 1。値を書き換えるコードは無い（所有者が `.env` を編集する） |
| 読み手（コード） | 11 | `workers/pipeline/run_worker.py` 3、`workers/production_image/run_worker.py` 1、`workers/production_video/run_worker.py` 1、`infrastructure/production/provider_credits.py` 1、`scripts/provider-credits.py` 1、`infrastructure/release/collectors.py` 4（release preflight が `.env` の生の値を読む。上限の判定には使わない） |
| 文書 | 10 | ADR-0057 1、ADR-0062 1、`docs/invariants.md` 3、`docs/operations/daily-credit-hold.md` 3、`docs/operations/release-runbook.md` 2 |
| 言及のみ（docstring） | 1 | `domain/errors.py` |
| 無関係 | 0 | |

上限の**判定**に使う入口は 1 つ: `Settings.fal_budget` → `evaluate_budget` → `BudgetDecision.cap_usd`
（worker 3 つが起動時に 1 回読み、`PaidJobRunner` に渡す）。`provider-credits.py` の `resume` は同じ設定値を
自分で読むが、合計の定義は `month_counted` に共有する。`reserve` を呼ぶ fal の本番コードは
`infrastructure/production/paid_job.py` の 1 箇所（`grep -rn "\.reserve(" apps/ workers/ domain/ infrastructure/ scripts/`
で、他は research / upload / planning / storyboard / scene recovery の fal 以外の provider）。
