# ADR-0058: 日次の事前診断・残高不足の保留・watchdog の分類

## Status

Proposed (2026-10-25)。ADR-0057（fal 障害耐性: breaker・保留・再開）と対になる。
`contracts/provider_health.py` の語彙（`ProviderHealthReader` / `CreditHoldWriter` / `HoldReason` /
`MAX_HELD_EPISODES`）を共有する。

## Context

実際に起きたこと（2026-10、本番）:

1. **Codex の model 不一致で日次が 7 日連続（〜10-24）失敗**。worker の `CODEX_MODEL` は空で、実効 model は
   ホストの対話用 `~/.codex/config.toml`（bind mount、共有）の `model = "gpt-6-sol"`。ChatGPT アカウントで
   HTTP 400（`docs/operations/codex-model-config.md`）。ADR-0054 の watchdog は `DAILY_WORKFLOW_FAILED`
   を出せるが、**原因が「model」だと分かるのは翌朝以降**で、毎朝同じ失敗を再試行していた。
   `CODEX_ALLOWED_MODELS`（PR #10）は既定が空で、運用者が実測しない限り何も止めない。
2. **fal の 403 が 22 回（10-13）。原因（残高か鍵か）は未確定**。残高が尽きても、日次は毎朝 Episode を作り、
   台本まで進み、production で同じ有料呼び出しを繰り返して失敗する構造だった（ADR-0057 の Context）。
3. 日次 workflow が「completed」でも、動画は無い。`DailyEpisodeWorkflow` は子 pipeline を起動して終わる
   だけ（`DailyOutcome.STARTED`）で、`EpisodePipelineWorkflow` も駐機点以外を返すと `STOPPED` で
   **Temporal 上は completed** になる。Temporal の completed は成功ではない（ADR-0031）が、
   「保留」という第三の状態は語彙に無く、`STOPPED` で表すと `PIPELINE_OUTCOME_MISMATCH`（=失敗）に見える。

調査で確認できたこと（読み取りのみ。利用枠は使っていない）:

- `codex debug models`（ネットワークのカタログ取得。prompt なし）の結果は `models_cache.json` と同じ 9 件。
  **gpt-6-sol も載っている**。カタログに載っていることは利用権の証拠にならない（既知）。
- 一方、ホストの codex 0.163.0（VS Code）の**対話セッションの記録**（`~/.codex/state_5.sqlite` の
  threads、`task_complete` イベント）に、同アカウントで `gpt-6-sol`（2 スレッド、計 8.2M tokens、
  10-18 22:13〜22:21）と `gpt-5.6-terra`（1 スレッド、2.6M tokens）が**完了している**実績がある。
  `gpt-5.5` は同日 0 tokens（拒否された記録と整合）。つまり**アカウントは gpt-6-sol を使える**。
  拒否は **client 版（コンテナの codex 0.154.0 とホスト 0.163.0）**または sandbox / 呼び出し経路の違いに
  由来する可能性が高い。ただし証明していない（実測は所有者承認の probe が必要）。

## Decision

**日次の入口で「走らせてよいか」を読み取りだけで判定し、その結果で実行の*モード*を決める。Schedule は
常に発火する。残高不足は障害ではなく管理された保留として記録し、失敗とも成功とも区別する。**

### 1. Codex の model を明示し、実行前に検査する（INV-75）

- model 設定の宣言元を compose の `x-codex-env` 1 箇所にする（3 つの worker が同じ
  `CODEX_MODEL` / `CODEX_ALLOWED_MODELS` / `CODEX_REQUIRE_PINNED_MODEL` を読む。以前は 3 サービスに同じ 4 行が
  書かれていた。`docker compose config` の描画結果は新キー以外同一）。
- 純粋関数 `domain/codex_preflight.py::evaluate_codex_model` が、設定された model・許可リスト・ローカル
  catalog（`models_cache.json`）から判定する。**prompt を送らない**。判定:
  `OK`（許可リストにあり catalog にもある）/ `UNVERIFIED`（catalog にあるが実測の証拠が無い。**既定**）/
  `CATALOG_UNREADABLE`（読めない・7 日超。「無い」と言わない）/ 以下は**必ず失敗する構成**として
  *blocking*: `NOT_IN_CATALOG` / `NOT_ALLOWED`（許可リスト外）/ `NOT_PINNED`（`CODEX_REQUIRE_PINNED_MODEL`
  なのに `CODEX_MODEL` が空）。
- script worker の Activity `codex_model_preflight` が答え、`DailyEpisodeWorkflow` が日次の入口
  （`patched`）で呼ぶ。blocking なら **Codex を呼ばず**に non-retryable で Daily を失敗させる
  （原因が failure_message に残り、`DAILY_WORKFLOW_FAILED`＝インフラ障害として出る）。Activity が
  応答できない（旧 worker 等）ときは検査せずに進む（「確認できなかった」を OK にはしないが、止める根拠もない）。
- `scripts/codex-model-preflight.py` で同じ判定を手元で実行できる（exit 2 = blocking）。
- **現在の本番設定の判定は `UNVERIFIED`**（カタログにはある）。これは正しい: 実際に 400 になった構成を
  事前診断だけでは区別できない。区別できるのは、所有者の probe（利用枠を 1 回使う）で実測した model を
  `CODEX_ALLOWED_MODELS` に書いた後（それ以外の model は `NOT_ALLOWED` で止まる）。

### 2. 日次の入口ゲート（INV-76）

`DailyEpisodeWorkflow` の `paused` 確認の直後（`workflow.patched("daily-preflight-0058")`）に、pipeline
worker の Activity `pipeline_daily_gate` を呼ぶ。読むもの（すべて読み取り）: provider breaker
（`ProviderHealthReader.snapshot`、`PROVIDER_FAMILY` から導いたアカウント）、未解放の保留数
（`open_hold_count`）、未照合予約数（`ProviderReservationRepository.list_unreconciled`）。DB を読めること自体が
DB 健全性の確認（読めなければ Activity が再試行し、Daily が失敗として残る）。結果:

| mode | 条件 | Daily の動き |
|---|---|---|
| `skip_backlog` | `open_hold_count >= MAX_HELD_EPISODES` | **何も作らない**（Planner も Codex も呼ばない）。`DailyOutcome.HELD_BACKLOG` |
| `paid_held` | breaker が closed でない | 通常どおり Planner → claim → pipeline。pipeline が production の前で保留する |
| `normal` | それ以外 | 従来どおり |

`half_open`（所有者承認の下での上限付き再開）も日次は使わない（`paid_held` 扱い）。未照合予約があっても
新しい Episode は止めない（INV-67 は入力単位。別の入力に影響しない）。数は結果に残す（見えるようにする）。

### 3. production の前の保留（INV-76）

`EpisodePipelineInput.credit_gate`（既定 False）が True の pipeline（Daily が起動したものだけ）は、
PRODUCTION 工程の前に Activity `pipeline_production_gate` を呼ぶ。breaker が closed でなければ
`CreditHoldWriter.hold_episode(scene_id=None, resumable_stage="production")` で保留を記録し、
`PipelineOutcome.HELD` を返して止まる（`STOPPED` ではない。子 workflow は起動しない）。

- 保留理由: breaker の reason が `balance_exhausted` → `WAITING_FOR_CREDITS`、それ以外
  （auth / permission / unknown）→ `PROVIDER_REFUSAL_UNKNOWN`。fal の 403 のように原因が確定できない
  ものを「残高不足」と断定しない。
- **Episode の状態は変えない**（`storyboard_ready` のまま。`storyboard_ready → blocked` は遷移表に無く、
  表を変えるとコードより先に状態機械の ADR が要る）。出口は「次工程の開始（production workflow が
  `STAGE_ADMITTED` を呼ぶ）」で既にあり、保留の*理由*は保留テーブルが持つ。ADR-0057 の production 内の
  保留（`in_progress → blocked`）とは状態が違うが、どちらも保留テーブルの未解放行が同じ意味を持つ。
- 判別を入力フラグに置くので、旧 workflow 履歴（フラグ無し=False）は新しい分岐を通らない。
  API / ADR-0032 の再開経路が起動する pipeline は `credit_gate=False` のままで、本ゲートに止められない
  （再開の可否は再開経路が持つ。日次は再開しない）。
- ゲートが保留した結果は `PipelineOutcome.HELD`。**成功ではない**（`COMPLETED` ではなく、
  `STARTED` でもない）。

### 4. 未完了 Episode の扱い（INV-78）

日次は**過去の Episode を再開しない**。`start_stage` は常に SCRIPT、再開の入口は ADR-0032 / ADR-0057 の
再開経路だけ。breaker が closed でない間・未照合予約がある間、日次は有料の再開を一切しない。翌日の日次は
新しい slot・新しい Episode を取る（INV-13）。保留が `MAX_HELD_EPISODES` に達したら（所有者が解放するまで）
新しい Episode も作らない。したがって breaker が開いたまま 7 日経っても、作られる Episode は
高々 `MAX_HELD_EPISODES`、有料呼び出しは 0 回。

### 5. watchdog の分類（INV-77）

watchdog は 3 つを区別する。**失敗**（インフラ: Daily が失敗、Schedule 異常）・**保留**（意図した停止）・
**未納品**（日数を超えた停滞）。

- 保留中の Episode を、停滞（`EPISODE_STAGE_STALLED`）と完成期限（`EPISODE_NOT_COMPLETED_BY_DEADLINE`）の
  検査から除く（意図した停止を失敗として通報しない）。保留は毎時の検査で `WatchdogResult.held_*` と
  WARNING ログ（`HELD_EPISODES`）に出る＝見えなくならない。
- 保留が `DEFAULT_HOLD_ESCALATION_DAYS`（3 日）を超えたら、**既存の `EPISODE_STAGE_STALLED`** を
  `detail.hold` 付きで記録し、既存の通知経路に乗せる。**新しい AnomalyKind を足さない**
  （`operational_anomalies.kind` の CHECK を広げない。release preflight は `drop_constraint` を止め、
  0017 の問題が未解決のため）。保留が解放されれば既存の自動 resolve で閉じる。
- `DailyOutcome` / `PipelineOutcome` は DB に保存しない（Workflow の戻り値のみ）ので、`HELD_BACKLOG` /
  `HELD` の追加に migration は要らない。
- 保留の読み出しは新しい最小 Protocol `OpenHoldReader`（`contracts/daily_gate.py`）。
  `ProviderHealthReader` は件数しか返さず、どの Episode が保留かを知る手段が無いため。
  実装は保留テーブルの未解放行を返すだけ（ADR-0057 側）。

### 6. replay 安全性（INV-78）

- `DailyEpisodeWorkflow` の新しい分岐は `workflow.patched("daily-preflight-0058")`。旧履歴は旧経路。
- `EpisodePipelineWorkflow` は入力フラグ（`credit_gate`）で分岐（上記）。
- `DailyWatchdogWorkflow` は**変更しない**（検査は 1 つの Activity。新しい挙動は Activity の中。
  `WatchdogCheckRequest` / `WatchdogResult` に既定値つきの項目を足すだけ）。
- 記録済みの履歴の replay テスト（リポジトリの fixture）に加え、本番 Temporal から読み取り専用で
  export した履歴を Replayer で検証する（`docs/testing/daily-resilience-rationale.md`）。

## §8 の確認（新しい設定キー・語彙の読み手 / 書き手）

`grep -rn "CODEX_REQUIRE_PINNED_MODEL\|codex_require_pinned" apps/ workers/ domain/ infrastructure/
contracts/ docs/ scripts/ compose.yaml .env.example`（実装時点、ADR 自身とテストを除く）: **11 件**。宣言（語彙）2: `contracts/codex_models.py`。
値の供給 3: `compose.yaml` 2（`x-codex-env` の定義と参照コメント）、`.env.example` 1。読み手 4:
`infrastructure/config.py`、`workers/planning/run_worker.py`、`scripts/codex-model-preflight.py` 2。
文書 2: `docs/invariants.md`、`docs/operations/codex-model-config.md`。無関係 0。
許可リストの語彙は従来どおり `contracts/codex_models.py` のみ。

## Alternatives

**(a) breaker が開いたら日次を丸ごとスキップする** — 台本・企画（Codex、無課金）まで止まり、残高が戻った
日に何も用意が無い。企画・台本は有料 API を使わないので進めてよい。却下。

**(b) 保留 Episode を `blocked` にする（ADR-0057 の方針どおり）** — 日次の入口は production の前で
`storyboard_ready`。`storyboard_ready → blocked` は遷移表に無く、足すなら状態機械の設計変更が先に要る。
`storyboard_ready` の出口は既にあり、保留の理由は別テーブルで足りる。却下（production 内の保留は B 側で
`in_progress → blocked`）。

**(c) 新しい AnomalyKind（`CREDIT_HOLD` 等）を足す** — CHECK 制約の拡張が要り、release preflight が
`drop_constraint` を止める（未解決の 0017 問題を増やす）。既存の `EPISODE_STAGE_STALLED` に `detail.hold` を
載せれば通知経路も使える。却下。

**(d) pipeline に patched ではなく常時ゲートを入れる** — 旧履歴・API 起動・再開経路の pipeline まで
止める（所有者承認の `half_open` 再開を日次のゲートが拒否する）。入力フラグなら起動元が決める。却下。

**(e) Codex の可否を `codex exec` の小さな probe で毎朝確認する** — 利用枠を毎日消費し、AGENTS §9 の
「有料・外部の副作用をテスト・自動処理から増やさない」に反する。読み取りの診断だけを自動化し、実測は
所有者承認の 1 回に限る。却下。

**(f) 失敗した日次を自動で翌日にやり直す** — 同じ失敗を毎日繰り返す（今回の 7 日連続）。保留と backlog 上限で
有限にする。却下。

## Consequences

**良い側**

- Codex の設定ミスが、Codex を呼ぶ前に・原因つきで検知される（許可リスト設定後）。model の出所が
  compose の 1 箇所になる
- 残高不足の日は失敗にならず、Episode は高々 `MAX_HELD_EPISODES` 個、有料呼び出しは繰り返されない
- 保留は失敗でも成功でもなく、毎時の watchdog の出力に見え、長引けば既存の経路で通報される

**悪い側 / 引き受けた負債**

- **現状の本番設定（`CODEX_MODEL` 空 + config.toml の gpt-6-sol）は事前診断で `UNVERIFIED`**。直るのは
  所有者の probe と設定変更（`docs/operations/codex-model-config.md`）の後
- ゲートの実装（breaker・保留・未解放行の読み出し）は ADR-0057 側。結線するまで日次は従来どおり
  （`NullProviderHealth` 相当。起動時に警告を出す）
- 保留 Episode は `storyboard_ready` のまま残る。人が `episodes` 一覧だけ見ると「止まっている」ように
  見える（保留の理由は保留テーブル・watchdog の出力・API の診断で見る）
- 保留の読み出し（`OpenHoldReader`）が失敗したときは、保留を除外できず従来の停滞検査が出る（誤報側に倒れる）
- 日次が `skip_backlog` で何も作らない日は `DAILY_*` の異常にならない（意図した停止）。見えるのは
  `WatchdogResult.held_*`・ログ・Daily の戻り値のみ
- Activity `codex_model_preflight` を持たない旧 script worker では検査が効かない（応答なしは進む）
- `UNVERIFIED` を「止める」設定（`CODEX_REQUIRE_PINNED_MODEL` + 許可リスト）は運用者が有効にするまで効かない
